"""Paper-forward signal log: the system's forward-looking truth.

Daily runs record hypothetical signals (no orders are placed anywhere) and
resolve them under the standard cost model. Monthly, forward reality is
tested numerically against what the backtest predicted; sustained divergence
pauses acceptance of new parameter changes.
"""
import datetime as dt
import json
import math
import pathlib

import pandas as pd

from src.backtest import load_intrabar, load_settings, simulate_trade, run_backtest
from src.genome import assemble, load_genome
from src.regime import validate_active

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "results" / "forward_log" / "signals.jsonl"
DIVERGENCE = ROOT / "results" / "forward_log" / "divergence.jsonl"


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    with path.open("a") as f:  # append, never overwrite: this is the audit trail
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def log_signals(df15: pd.DataFrame, df1h: pd.DataFrame, settings: dict) -> dict:
    """Append new hypothetical signals from the active parameters, then try to
    resolve any still-open ones against the newly available candles."""
    active = validate_active()
    active_file = json.loads((ROOT / active["params_file"]).read_text())
    params = active_file["params"]
    genome = load_genome(active_file.get("genome", "baseline"))
    existing = _read_jsonl(LOG)
    seen = {r["time"] for r in existing if r["kind"] == "signal"}
    resolved = {r["signal_time"] for r in existing if r["kind"] == "resolution"}

    entry_fn, _ = assemble(genome)
    signals = entry_fn(df15, df1h, params, settings["strategy_fixed"])
    horizon = df15.index[-1] - pd.Timedelta(days=10)
    fresh = [s for s in signals if s["time"] >= horizon and str(s["time"]) not in seen]
    new_records = [{"kind": "signal", "time": str(s["time"]), "direction": s["direction"],
                    "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                    "params_file": active["params_file"],
                    "logged": dt.date.today().isoformat()} for s in fresh]

    idx = {str(t): i for i, t in enumerate(df15.index)}
    intrabar = load_intrabar()
    open_signals = [r for r in existing + new_records
                    if r["kind"] == "signal" and r["time"] not in resolved]
    resolutions = []
    for rec in open_signals:
        i = idx.get(rec["time"])
        if i is None:
            continue
        sig = {"time": pd.Timestamp(rec["time"]), "direction": rec["direction"],
               "entry": rec["entry"], "stop": rec["stop"], "target": rec["target"]}
        trade = simulate_trade(df15, i, sig, settings, intrabar, genome["exit_style"])
        if trade is None:
            resolutions.append({"kind": "resolution", "signal_time": rec["time"],
                                "outcome": "unfilled", "pnl_pips": 0.0,
                                "resolved": dt.date.today().isoformat()})
        elif trade["exit_time"] != str(df15.index[-1]):  # still open at data edge: wait
            resolutions.append({"kind": "resolution", "signal_time": rec["time"],
                                "outcome": "closed", "pnl_pips": trade["pnl_pips"],
                                "resolved": dt.date.today().isoformat()})
    _append_jsonl(LOG, new_records + resolutions)
    return {"new_signals": len(new_records), "resolved": len(resolutions)}


def decay_check(settings: dict) -> dict:
    """Monitoring decay alert — deliberately tighter than the Evaluator's gate,
    so the system reacts before the situation is bad enough to fail it."""
    mon = settings["monitor"]
    closed = [r for r in _read_jsonl(LOG)
              if r["kind"] == "resolution" and r["outcome"] == "closed"]
    recent = [r["pnl_pips"] for r in closed[-mon["expectancy_window_trades"]:]]
    expectancy = sum(recent) / len(recent) if recent else 0.0
    equity, peak, dd = 0.0, 0.0, 0.0
    start = settings["backtest"]["start_equity_pips"]
    for r in closed:
        equity += r["pnl_pips"]
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / (start + peak))
    trigger = (len(recent) >= mon["expectancy_window_trades"] and expectancy < 0) \
        or dd > mon["drawdown_alert"]
    return {"expectancy_recent": round(expectancy, 3), "drawdown": round(dd, 4),
            "decay_trigger": trigger}


def divergence_check(df15: pd.DataFrame, df1h: pd.DataFrame, settings: dict) -> dict:
    """Monthly, numeric: last N forward signals vs the backtest's prediction
    for the same parameters. Two consecutive alerts pause acceptance."""
    div = settings["divergence"]
    closed = [r for r in _read_jsonl(LOG)
              if r["kind"] == "resolution" and r["outcome"] == "closed"]
    sample = [r["pnl_pips"] for r in closed[-div["forward_sample_trades"]:]]
    if len(sample) < div["forward_sample_trades"]:
        return {"skipped": f"only {len(sample)} closed forward signals"}
    fwd_exp = sum(sample) / len(sample)
    fwd_wr = sum(1 for p in sample if p > 0) / len(sample)

    active = validate_active()
    active_file = json.loads((ROOT / active["params_file"]).read_text())
    params = active_file["params"]
    genome = load_genome(active_file.get("genome", "baseline"))
    bt = run_backtest(df15, df1h, params, settings, load_intrabar(), genome=genome)
    pnls = [t["pnl_pips"] for t in bt["trades"]]
    if not pnls:
        return {"skipped": "backtest produced no trades to compare against"}
    bt_exp = sum(pnls) / len(pnls)
    bt_wr = bt["metrics"]["win_rate"]
    var = sum((p - bt_exp) ** 2 for p in pnls) / max(len(pnls) - 1, 1)
    se = math.sqrt(var / len(pnls))
    band = div["expectancy_se_multiplier"] * se
    exp_alert = abs(fwd_exp - bt_exp) > band
    wr_alert = abs(fwd_wr - bt_wr) > div["win_rate_max_delta"]
    record = {"date": dt.date.today().isoformat(),
              "forward_expectancy": round(fwd_exp, 3), "backtest_expectancy": round(bt_exp, 3),
              "expectancy_band": round(band, 3), "forward_win_rate": round(fwd_wr, 4),
              "backtest_win_rate": bt_wr, "expectancy_alert": exp_alert,
              "win_rate_alert": wr_alert, "alert": exp_alert or wr_alert}
    _append_jsonl(DIVERGENCE, [record])
    return record


def acceptance_paused(settings: dict) -> bool:
    """True when the last two monthly divergence checks both alerted: the
    backtest is no longer predicting reality, so its verdicts are not trusted."""
    records = [r for r in _read_jsonl(DIVERGENCE) if "alert" in r]
    n = settings["divergence"]["consecutive_alerts_to_pause"]
    return len(records) >= n and all(r["alert"] for r in records[-n:])


if __name__ == "__main__":
    import argparse
    from src.data import load_candles
    ap = argparse.ArgumentParser(description="Crucible paper-forward log")
    ap.add_argument("--log", action="store_true", help="append & resolve daily signals")
    ap.add_argument("--divergence", action="store_true", help="monthly forward-vs-backtest check")
    args = ap.parse_args()
    settings = load_settings()
    df15, df1h = load_candles()
    if args.log:
        out = log_signals(df15, df1h, settings)
        out["decay"] = decay_check(settings)
        print(json.dumps(out, indent=2))
    if args.divergence:
        print(json.dumps(divergence_check(df15, df1h, settings), indent=2))
