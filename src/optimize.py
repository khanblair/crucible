"""Optuna Bayesian search over the strategy's tunable parameters.

The optimizer only ever sees the training window: the final six months are
permanently quarantined (holdout), the rolling out-of-sample window is
reserved for the Evaluator, and an embargo gap separates train from test so
indicator lookbacks cannot leak across the boundary. DeepSeek is not involved
in this step. Math does the math.
"""
import datetime as dt
import json
import pathlib

import optuna
import pandas as pd

from src.backtest import load_intrabar, load_settings, phase0_passed, run_backtest
from src.regime import classify, is_stale, validate_active

ROOT = pathlib.Path(__file__).resolve().parent.parent


def split_windows(df15: pd.DataFrame, settings: dict) -> dict:
    """Holdout (untouched) | embargo | out-of-sample | embargo | training."""
    wf = settings["walk_forward"]
    end = df15.index[-1]
    holdout_start = end - pd.DateOffset(months=wf["holdout_months"])
    oos_end = holdout_start - pd.Timedelta(days=1)
    oos_start = oos_end - pd.Timedelta(days=wf["oos_days"])
    train_end = oos_start - pd.Timedelta(days=wf["embargo_days"])
    return {"train_end": train_end, "oos_start": oos_start, "oos_end": oos_end,
            "holdout_start": holdout_start}


def regime_dates(df1h: pd.DataFrame, settings: dict, regime: str) -> set:
    """Days on which the deterministic classifier labels the market as
    `regime` — training windows are filtered to the regime being optimized."""
    dates = set()
    days = pd.Series(df1h.index.date).unique()
    for day in days[settings["regime"]["atr_window_days"]:]:
        upto = df1h[df1h.index.date <= day]
        if classify(upto, settings)["regime"] == regime:
            dates.add(day)
    return dates


def target_regime(df1h: pd.DataFrame, settings: dict) -> str:
    """A stale regime set is revalidated first, regardless of which regime
    prevails on Sunday; otherwise optimize the prevailing regime's set."""
    for name in ("trending", "ranging", "high_volatility", "low_volatility"):
        if is_stale(name, settings):
            return name
    return classify(df1h, settings)["regime"]


def search(df15: pd.DataFrame, df1h: pd.DataFrame, settings: dict,
           intrabar: dict, allowed_dates: set | None, n_trials: int,
           timeout_s: float, genome: dict | None = None) -> tuple[dict, dict]:
    """Bayesian search on the training window only, within a fixed genome
    (defaults to baseline). Returns (params, train metrics)."""
    space = settings["search_space"]

    def objective(trial: optuna.Trial) -> float:
        params = {k: trial.suggest_float(k, lo, hi) for k, (lo, hi) in space.items()}
        if params["rsi_buy_low"] >= params["rsi_buy_high"] or \
           params["rsi_sell_low"] >= params["rsi_sell_high"]:
            return float("-inf")
        m = run_backtest(df15, df1h, params, settings, intrabar, allowed_dates,
                         genome)["metrics"]
        if m["n_trades"] < settings["gates"]["min_oos_trades"]:
            return float("-inf")
        trial.set_user_attr("metrics", {k: v for k, v in m.items() if k != "equity_curve"})
        return m["net_profit_pips"]

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=7))
    study.optimize(objective, n_trials=n_trials, timeout=timeout_s)
    if study.best_value == float("-inf"):
        raise RuntimeError("no viable candidate found in the search space")
    return study.best_params, study.best_trial.user_attrs["metrics"]


def _main() -> None:
    if not phase0_passed():
        print("Phase 0 has not passed (see docs/phase0_report.md) — the optimization "
             "loop must not run against a strategy with no proven edge. Exiting cleanly.")
        return
    settings = load_settings()
    active = validate_active()  # every run begins by validating the pointer
    from src.data import load_candles
    df15, df1h = load_candles()
    intrabar = load_intrabar()
    windows = split_windows(df15, settings)
    regime = target_regime(df1h, settings)
    train15 = df15[df15.index <= windows["train_end"]]
    train1h = df1h[df1h.index <= windows["train_end"]]
    if len(train15) < 90 * 96:  # holdout + OOS + embargo must leave >=90 days to train on
        raise RuntimeError(f"training window too small ({len(train15)} bars) — "
                           "extend the historical dataset before optimizing")
    allowed = regime_dates(train1h, settings, regime) or None
    cfg = settings["optuna"]
    genome = json.loads((ROOT / "config" / "regimes" / f"{regime}.json").read_text())["genome"]
    from src.genome import load_genome
    genome_def = load_genome(genome)
    params, train_metrics = search(train15, train1h, settings, intrabar, allowed,
                                   cfg["n_trials"], cfg["timeout_minutes"] * 60, genome_def)
    candidate = {
        "date": dt.date.today().isoformat(), "type": "candidate", "regime": regime,
        "genome": genome, "params": params, "train_metrics": train_metrics,
        "baseline_file": f"config/regimes/{regime}.json",
        "oos_start": str(windows["oos_start"]), "oos_end": str(windows["oos_end"]),
        "holdout_start": str(windows["holdout_start"]), "evaluated": False,
    }
    out = ROOT / "results" / "runs" / f"candidate_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps(candidate, indent=2) + "\n")
    print(json.dumps({"regime": regime, "params": params,
                      "train_metrics": train_metrics}, indent=2))


if __name__ == "__main__":
    _main()
