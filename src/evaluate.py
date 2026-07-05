"""The Evaluator: every accept/reject gate. Human-edited only.

A separate, deterministic set of checks run on out-of-sample data the
optimizer never saw. Every condition must pass; a single failure discards
the candidate. Candidate and baseline are always compared on the same feed.
"""
import datetime as dt
import json
import pathlib

import pandas as pd

from src.backtest import load_intrabar, load_settings, run_backtest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------- gates
# Every gate returns bool(...) explicitly: inputs derived from pandas/numpy
# (e.g. via .iloc[]) stay numpy.float64 through arithmetic, and a bare
# comparison of two numpy floats yields numpy.bool_, not a native bool.
# json.dumps cannot serialize numpy.bool_ — this bit in production once
# already (see backtest.py's harness-boundary cast for the matching fix).
def gate_improvement(candidate_net: float, baseline_net: float, min_rel: float) -> bool:
    """Meaningful improvement, not marginal. A point-estimate tie goes to
    the incumbent."""
    if baseline_net > 0:
        return bool(candidate_net > baseline_net * (1.0 + min_rel))
    if baseline_net < 0:
        return bool(candidate_net > baseline_net + abs(baseline_net) * min_rel)
    return bool(candidate_net > 0.0)


def gate_trades(n_trades: int, floor: int) -> bool:
    """Statistical floor: results built on a handful of trades are noise."""
    return bool(n_trades >= floor)


def gate_drawdown(max_dd: float, ceiling: float) -> bool:
    return bool(max_dd < ceiling)


def gate_win_rate(win_rate: float, floor: float) -> bool:
    return bool(win_rate > floor)


def gate_consistency(test_net: float, train_net: float, max_dropoff: float) -> bool:
    """A sharp train->test collapse is the signature of overfitting."""
    if train_net <= 0:
        return True  # nothing to collapse from; other gates still apply
    return bool(test_net >= train_net * (1.0 - max_dropoff))


def evaluate_candidate(candidate_oos: dict, baseline_oos: dict, candidate_train: dict,
                       settings: dict, min_relative_improvement: float | None = None) -> dict:
    """Run every gate. Returns {accepted, gates: [{name, passed, detail}]}.

    `min_relative_improvement` overrides the normal parameter-change threshold
    — evolve.py passes the stricter `structural_min_relative_improvement` for
    genome candidates, since changing logic is a bigger commitment than
    tuning a number."""
    g = settings["gates"]
    threshold = g["min_relative_improvement"] if min_relative_improvement is None \
        else min_relative_improvement
    gates = [
        ("improvement", gate_improvement(candidate_oos["net_profit_pips"],
                                         baseline_oos["net_profit_pips"], threshold),
         f"candidate {candidate_oos['net_profit_pips']} vs baseline "
         f"{baseline_oos['net_profit_pips']} (+{threshold:.0%} required)"),
        ("trade_floor", gate_trades(candidate_oos["n_trades"], g["min_oos_trades"]),
         f"{candidate_oos['n_trades']} trades (floor {g['min_oos_trades']})"),
        ("drawdown_ceiling", gate_drawdown(candidate_oos["max_drawdown"], g["max_drawdown"]),
         f"{candidate_oos['max_drawdown']:.2%} (ceiling {g['max_drawdown']:.0%})"),
        ("win_rate_floor", gate_win_rate(candidate_oos["win_rate"], g["min_win_rate"]),
         f"{candidate_oos['win_rate']:.2%} (floor {g['min_win_rate']:.0%})"),
        ("consistency", gate_consistency(candidate_oos["net_profit_pips"],
                                         candidate_train["net_profit_pips"],
                                         g["max_consistency_dropoff"]),
         f"test {candidate_oos['net_profit_pips']} vs train "
         f"{candidate_train['net_profit_pips']} pips"),
    ]
    return {"accepted": all(p for _, p, _ in gates),
            "gates": [{"name": n, "passed": p, "detail": d} for n, p, d in gates]}


# ----------------------------------------------------- cooldown & champion
def cooldown_clear(settings: dict, today: dt.date | None = None) -> bool:
    """No change may be accepted within N trading days of the previous one."""
    today = today or dt.date.today()
    accepted = sorted((ROOT / "results" / "runs").glob("decision_*.json"))
    last = None
    for path in accepted:
        rec = json.loads(path.read_text())
        if rec.get("accepted"):
            last = dt.date.fromisoformat(rec["date"])
    if last is None:
        return True
    trading_days = pd.bdate_range(last, today).size - 1
    return trading_days >= settings["cooldown_trading_days"]


def champion_check(df15, df1h, settings: dict, intrabar: dict) -> dict:
    """Replay Champion Zero vs the evolved system over accumulated data —
    system-versus-system, under identical execution costs. Each side runs
    under its own genome: Champion Zero's is frozen forever; the evolved
    system's may differ after a genome-evolution PR merges."""
    from src.genome import load_genome
    champion = json.loads((ROOT / "config" / "champion_zero.json").read_text())
    active = json.loads((ROOT / "config" / "active.json").read_text())
    evolved = json.loads((ROOT / active["params_file"]).read_text())
    champion_genome = load_genome(champion["genome"])
    evolved_genome = load_genome(evolved.get("genome", champion["genome"]))
    zero = run_backtest(df15, df1h, champion["params"], settings, intrabar,
                        genome=champion_genome)
    curr = run_backtest(df15, df1h, evolved["params"], settings, intrabar,
                        genome=evolved_genome)

    def trailing_net(trades: list[dict], days: int) -> float:
        cutoff = df15.index[-1] - pd.Timedelta(days=days)
        return round(sum(t["pnl_pips"] for t in trades
                         if pd.Timestamp(t["time"]) >= cutoff), 4)

    ch = settings["champion"]
    warn = trailing_net(curr["trades"], ch["warning_window_days"]) < \
        trailing_net(zero["trades"], ch["warning_window_days"])
    suspend = trailing_net(curr["trades"], ch["suspension_window_days"]) < \
        trailing_net(zero["trades"], ch["suspension_window_days"])
    zm, cm = zero["metrics"], curr["metrics"]
    keys = ("net_profit_pips", "win_rate", "max_drawdown", "n_trades")
    return {"champion_zero": {k: zm[k] for k in keys}, "evolved": {k: cm[k] for k in keys},
            "champion_zero_equity": zm["equity_curve"], "evolved_equity": cm["equity_curve"],
            "evolved_leads": cm["net_profit_pips"] >= zm["net_profit_pips"],
            "warning_90d": warn, "suspend_180d": suspend}


# --------------------------------------------------------------- main flow
def _main() -> None:
    settings = load_settings()
    candidates = sorted((ROOT / "results" / "runs").glob("candidate_*.json"))
    if not candidates:
        print("no candidate to evaluate")
        return
    cand = json.loads(candidates[-1].read_text())
    if cand.get("evaluated"):
        print("latest candidate already evaluated")
        return
    from src.data import load_candles
    df15, df1h = load_candles()
    intrabar = load_intrabar()
    oos_start = pd.Timestamp(cand["oos_start"])
    oos_end = pd.Timestamp(cand["oos_end"])
    m15 = df15[(df15.index >= oos_start) & (df15.index <= oos_end)]
    m1h = df1h[df1h.index <= oos_end]
    from src.genome import load_genome
    baseline_params = json.loads((ROOT / cand["baseline_file"]).read_text())["params"]
    genome_def = load_genome(cand["genome"])
    cand_oos = run_backtest(m15, m1h, cand["params"], settings, intrabar,
                            genome=genome_def)["metrics"]
    base_oos = run_backtest(m15, m1h, baseline_params, settings, intrabar,
                            genome=genome_def)["metrics"]
    verdict = evaluate_candidate(cand_oos, base_oos, cand["train_metrics"], settings)
    champion = champion_check(df15, df1h, settings, intrabar)
    from src.forward import acceptance_paused
    for name, blocked, detail in (
            ("cooldown", not cooldown_clear(settings), "cooldown window active"),
            ("divergence_pause", acceptance_paused(settings),
             "consecutive forward-divergence alerts: backtest verdicts not trusted"),
            ("champion_circuit_breaker", champion["suspend_180d"],
             "evolved system trails Champion Zero over the 180-day window")):
        if blocked:
            verdict = {"accepted": False, "gates": verdict["gates"] + [
                {"name": name, "passed": False, "detail": detail}]}
    decision = {"date": dt.date.today().isoformat(), "type": "decision",
                "regime": cand["regime"], "params": cand["params"],
                "baseline_params": baseline_params,
                "candidate_oos": {k: v for k, v in cand_oos.items() if k != "equity_curve"},
                "baseline_oos": {k: v for k, v in base_oos.items() if k != "equity_curve"},
                **verdict, "champion": champion}
    out = ROOT / "results" / "runs" / f"decision_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps(decision, indent=2) + "\n")
    cand["evaluated"] = True
    candidates[-1].write_text(json.dumps(cand, indent=2) + "\n")
    if decision["accepted"]:
        regime_file = ROOT / "config" / "regimes" / f"{cand['regime']}.json"
        # `genome` is preserved as-is: this write path only ever updates `params`
        # (direct commit); changing `genome` itself requires a pull request (evolve.py).
        regime_file.write_text(json.dumps({
            "name": cand["regime"], "genome": cand["genome"],
            "last_validated": dt.date.today().isoformat(),
            "validated_by": f"optimization {dt.date.today().isoformat()}",
            "params": cand["params"]}, indent=2) + "\n")
        active = json.loads((ROOT / "config" / "active.json").read_text())
        if active["regime"] == cand["regime"]:
            active["params_file"] = f"config/regimes/{cand['regime']}.json"
            active["fallback_champion_zero"] = False
            active["updated"] = dt.date.today().isoformat()
        (ROOT / "config" / "active.json").write_text(json.dumps(active, indent=2) + "\n")
    print(json.dumps({"accepted": decision["accepted"],
                      "gates": decision["gates"]}, indent=2))


if __name__ == "__main__":
    _main()
