"""Backtest harness: execution costs, slippage, swap, intrabar ordering, metrics.

The ONLY implementation of trade simulation in the system. optimize.py,
evaluate.py and forward.py all call into this module — one implementation,
zero drift between training, gating and forward results.
"""
import json
import pathlib

import pandas as pd

from src.genome import EXIT_STYLES, assemble
from src.strategy import PIP

ROOT = pathlib.Path(__file__).resolve().parent.parent
INTRABAR_PATH = ROOT / "data" / "intrabar" / "ordering.parquet"


def load_settings() -> dict:
    return json.loads((ROOT / "config" / "settings.json").read_text())


def load_intrabar(path: pathlib.Path = INTRABAR_PATH) -> dict:
    """Committed lookup table: bar timestamp -> 'high_first' | 'low_first'."""
    if not path.exists():
        return {}
    df = pd.read_parquet(path)
    return dict(zip(pd.to_datetime(df["time"]), df["first"]))


def simulate_trade(df15: pd.DataFrame, signal_idx: int, sig: dict, settings: dict,
                   intrabar: dict | None = None, exit_style: str = "atr_trail_half") -> dict | None:
    """Simulate one signal bar-by-bar. Returns a trade dict or None if never filled.

    Cost model (all net, in pips): round-trip spread once per trade, adverse
    slippage on entry and on every stop-loss exit (scaled by exited fraction),
    swap per rollover crossed (triple on the configured weekday). Trade
    *management* — when and how the position closes — is delegated to the
    chosen exit-style module; cost application here never changes per genome.
    """
    costs = settings["costs"]
    fixed = settings["strategy_fixed"]
    intrabar = intrabar or {}
    d = sig["direction"]
    entry = sig["entry"]

    fill_idx = None
    for j in range(signal_idx + 1, min(signal_idx + 1 + fixed["entry_valid_bars"], len(df15))):
        bar = df15.iloc[j]
        if (d == 1 and bar["high"] >= entry) or (d == -1 and bar["low"] <= entry):
            fill_idx = j
            break
    if fill_idx is None:
        return None

    legs = EXIT_STYLES[exit_style](df15, fill_idx, sig, settings, intrabar)
    fraction_open = 1.0 - sum(frac for _, frac, _, _ in legs)
    if fraction_open > 1e-9:  # end of data: mark remainder out at last close
        legs = legs + [(df15["close"].iloc[-1], fraction_open, False, len(df15) - 1)]
    exit_idx = legs[-1][3]

    rate = costs["swap_long_pips_per_day"] if d == 1 else costs["swap_short_pips_per_day"]
    swap_pips, remaining, prev_idx = 0.0, 1.0, fill_idx
    for _, frac, _, leg_idx in legs:
        for j in range(prev_idx + 1, leg_idx + 1):
            t = df15.index[j]
            if t.hour == costs["rollover_hour_utc"] and t.minute == 0:
                mult = 3.0 if t.weekday() == costs["triple_swap_weekday"] else 1.0
                swap_pips += rate * mult * remaining  # scaled by whatever fraction is still open
        remaining -= frac
        prev_idx = leg_idx

    gross = sum(d * (px - entry) / PIP * frac for px, frac, _, _ in legs)
    slip = costs["slippage_pips"] * (1.0 + sum(frac for _, frac, is_stop, _ in legs if is_stop))
    net = gross - costs["spread_pips"] - slip + swap_pips
    return {"time": str(sig["time"]), "direction": d, "entry": entry,
            "fill_time": str(df15.index[fill_idx]), "exit_time": str(df15.index[exit_idx]),
            "gross_pips": round(gross, 4), "spread_pips": costs["spread_pips"],
            "slippage_pips": round(slip, 4), "swap_pips": round(swap_pips, 4),
            "pnl_pips": round(net, 4)}


def compute_metrics(trades: list[dict], settings: dict) -> dict:
    n = len(trades)
    pnls = [t["pnl_pips"] for t in trades]
    net = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity = [settings["backtest"]["start_equity_pips"]]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    gp, gl = sum(wins), -sum(losses)
    return {"n_trades": n, "net_profit_pips": round(net, 4),
            "expectancy_pips": round(net / n, 4) if n else 0.0,
            "win_rate": round(len(wins) / n, 4) if n else 0.0,
            "profit_factor": round(gp / gl, 4) if gl > 0 else (float("inf") if gp > 0 else 0.0),
            "max_drawdown": round(max_dd, 4), "equity_curve": [round(e, 2) for e in equity]}


def run_backtest(df15: pd.DataFrame, df1h: pd.DataFrame, params: dict, settings: dict,
                 intrabar: dict | None = None, allowed_dates: set | None = None,
                 genome: dict | None = None) -> dict:
    """Full backtest of a strategy genome with the given parameters. `genome`
    defaults to the baseline structure (today's exact behavior) when omitted.
    One position at a time; signals during an open trade are skipped."""
    entry_fn, exit_fn = assemble(genome)
    exit_style = (genome or {}).get("exit_style", "atr_trail_half")
    signals = entry_fn(df15, df1h, params, settings["strategy_fixed"])
    if allowed_dates is not None:
        signals = [s for s in signals if s["time"].date() in allowed_dates]
    idx = {t: i for i, t in enumerate(df15.index)}
    trades, busy_until = [], None
    for sig in signals:
        if busy_until is not None and sig["time"] <= busy_until:
            continue
        trade = simulate_trade(df15, idx[sig["time"]], sig, settings, intrabar, exit_style)
        if trade:
            trades.append(trade)
            busy_until = pd.Timestamp(trade["exit_time"])
    metrics = compute_metrics(trades, settings)
    return {"metrics": metrics, "trades": trades}


def phase0_passed() -> bool:
    """Code-level backstop: the optimization and evolution loops must never
    run against a strategy that hasn't proven it has an edge, regardless of
    whether the workflows happen to be enabled. Manual GitHub Actions toggles
    are the primary control; this is the defense-in-depth check underneath."""
    report = ROOT / "docs" / "phase0_report.md"
    if not report.exists():
        return False
    return "**Verdict: PASS**" in report.read_text()


def _phase0() -> None:
    """Phase 0: full multi-year backtest of champion zero. Writes docs/phase0_report.md."""
    from src.data import load_candles
    settings = load_settings()
    champion = json.loads((ROOT / "config" / "champion_zero.json").read_text())
    df15, df1h = load_candles()
    result = run_backtest(df15, df1h, champion["params"], settings, load_intrabar())
    m, trades = result["metrics"], result["trades"]
    by_year: dict[int, list] = {}
    for t in trades:
        by_year.setdefault(pd.Timestamp(t["time"]).year, []).append(t["pnl_pips"])
    total = m["net_profit_pips"]
    max_share = max((sum(v) / total for v in by_year.values()), default=0.0) if total > 0 else 1.0
    p0 = settings["phase0"]
    passed = (m["expectancy_pips"] > 0 and m["profit_factor"] > p0["min_profit_factor"]
              and max_share <= p0["max_single_year_profit_share"])
    lines = ["# Phase 0 Report — Base Strategy Proof", "",
             f"**Verdict: {'PASS' if passed else 'FAIL'}**", "",
             f"- Expectancy per trade (net): {m['expectancy_pips']} pips",
             f"- Profit factor: {m['profit_factor']}",
             f"- Maximum drawdown: {m['max_drawdown']:.2%}",
             f"- Total trades: {m['n_trades']}",
             f"- Largest single-year profit share: {max_share:.2%}", "",
             "## Trades by year", ""]
    lines += [f"- {y}: {len(v)} trades, {sum(v):.1f} pips net" for y, v in sorted(by_year.items())]
    (ROOT / "docs" / "phase0_report.md").write_text("\n".join(lines) + "\n")
    print(f"Phase 0 {'PASS' if passed else 'FAIL'} — report written to docs/phase0_report.md")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Crucible backtest harness")
    ap.add_argument("--phase0", action="store_true", help="run the Phase 0 base-strategy proof")
    if ap.parse_args().phase0:
        _phase0()
