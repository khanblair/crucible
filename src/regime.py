"""Deterministic market regime classification: ADX/ATR precedence matrix.

Pure arithmetic on 1-hour Dukascopy data. No AI anywhere in this module.
Every classification's inputs, output and selected rule are logged so any
decision can be reproduced exactly.
"""
import datetime as dt
import json
import pathlib

import pandas as pd

from src.strategy import adx, atr, validate_params

ROOT = pathlib.Path(__file__).resolve().parent.parent

REGIMES = ("trending", "ranging", "high_volatility", "low_volatility")


def classify(df1h: pd.DataFrame, settings: dict) -> dict:
    """Apply the precedence matrix. Rules, in strict order:
    1. High Volatility outranks everything (ATR > high percentile).
    2. A quiet trend is still a trend (ADX > trend threshold).
    3. Low Volatility requires the absence of trend; neutral ADX with
       mid-range volatility defaults to Ranging.
    """
    cfg = settings["regime"]
    adx_now = float(adx(df1h, cfg["adx_period"]).iloc[-1])
    atr_series = atr(df1h, cfg["adx_period"])
    window = atr_series.iloc[-(cfg["atr_window_days"] * 24):]
    atr_now = float(atr_series.iloc[-1])
    pct = float((window < atr_now).mean() * 100)

    if pct > cfg["atr_high_percentile"]:
        label, rule = "high_volatility", "1: high volatility outranks everything"
    elif adx_now > cfg["adx_trend"]:
        label, rule = "trending", "2: a quiet trend is still a trend"
    elif pct < cfg["atr_low_percentile"]:
        label, rule = "low_volatility", "3: low volatility in the absence of trend"
    else:
        label, rule = "ranging", "3: neutral/no-trend with mid-range volatility"
    return {"regime": label, "rule": rule, "adx": round(adx_now, 2),
            "atr": round(atr_now, 6), "atr_percentile": round(pct, 1)}


def load_regime_params(regime: str) -> dict:
    return json.loads((ROOT / "config" / "regimes" / f"{regime}.json").read_text())


def is_stale(regime: str, settings: dict, today: dt.date | None = None) -> bool:
    """A set not re-validated within the staleness window is untrusted."""
    today = today or dt.date.today()
    validated = dt.date.fromisoformat(load_regime_params(regime)["last_validated"])
    return (today - validated).days > settings["regime"]["staleness_days"]


def validate_active() -> dict:
    """Every run begins here: the pointer must target an existing,
    schema-valid parameter file. Half-written configuration is fatal."""
    active = json.loads((ROOT / "config" / "active.json").read_text())
    target = ROOT / active["params_file"]
    if not target.exists():
        raise FileNotFoundError(f"active.json points to missing file: {active['params_file']}")
    validate_params(json.loads(target.read_text())["params"])
    return active


def apply(df1h: pd.DataFrame, settings: dict) -> dict:
    """Daily routing: if the prevailing regime changed, swap active.json to the
    matching pre-validated set the same day. Routing, not optimization — no new
    hypothesis is tested, so this lives outside the cadence and the cooldown."""
    active = validate_active()
    decision = classify(df1h, settings)
    regime = decision["regime"]
    stale = is_stale(regime, settings)
    changed = regime != active.get("regime") or stale != active.get("fallback_champion_zero")
    if changed:
        active = {
            "regime": regime,
            # stale sets are untrusted: champion zero is the safe fallback
            "params_file": ("config/champion_zero.json" if stale
                            else f"config/regimes/{regime}.json"),
            "fallback_champion_zero": stale,
            "updated": dt.date.today().isoformat(),
        }
        (ROOT / "config" / "active.json").write_text(json.dumps(active, indent=2) + "\n")
    log = {"date": dt.date.today().isoformat(), "type": "regime",
           "decision": decision, "stale": stale, "swapped": changed}
    out = ROOT / "results" / "runs" / f"regime_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps(log, indent=2) + "\n")
    return log


if __name__ == "__main__":
    import argparse
    from src.data import load_candles
    ap = argparse.ArgumentParser(description="Crucible regime classifier")
    ap.add_argument("--apply", action="store_true", help="classify and swap active.json if changed")
    args = ap.parse_args()
    settings = json.loads((ROOT / "config" / "settings.json").read_text())
    _, df1h = load_candles()
    result = apply(df1h, settings) if args.apply else classify(df1h, settings)
    print(json.dumps(result, indent=2))
