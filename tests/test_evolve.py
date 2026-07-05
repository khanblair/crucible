"""Genome-evolution tests: trigger-detection boundary cases, cooldown, and
the screen->refine ranking logic on tiny synthetic data. PR/git/gh
orchestration (open_pr, watch_prs) is exercised for real by evolve.yml and
pr_watch.yml in production — that's an integration concern, not a unit one.
"""
import datetime as dt
import json
import pathlib

import numpy as np
import pandas as pd
import pytest

from src.backtest import load_settings
from src.evolve import (_check_trigger, _relative_change, evolution_cooldown_clear,
                        genome_id_for, losing_streak_and_stuck, refine, run_evolution, screen)

RUNS = pathlib.Path("results/runs")


def _write_decision(day: str, baseline_net: float, candidate_net: float) -> pathlib.Path:
    path = RUNS / f"decision_{day}.json"
    path.write_text(json.dumps({"date": day, "baseline_oos": {"net_profit_pips": baseline_net},
                                "candidate_oos": {"net_profit_pips": candidate_net}}))
    return path


@pytest.fixture
def clean_runs():
    """Isolate these tests from any real results/runs/*.json using a
    reserved 2099-* date prefix, cleaned up before and after."""
    made = []
    yield made
    for path in made:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------- _relative_change
def test_relative_change_positive_baseline():
    assert _relative_change(100.0, 102.0) == pytest.approx(0.02)


def test_relative_change_negative_baseline():
    assert _relative_change(-10.0, -9.8) == pytest.approx(0.02)


def test_relative_change_zero_baseline():
    assert _relative_change(0.0, 5.0) == 1.0
    assert _relative_change(0.0, -5.0) == 0.0
    assert _relative_change(0.0, 0.0) == 0.0


# ------------------------------------------------------- losing_streak_and_stuck
def test_trigger_fires_on_losing_streak_plus_stuck_optimizer(clean_runs):
    settings = load_settings()
    n = settings["evolution"]["losing_streak_runs"]
    days = [f"2099-01-{10 + i:02d}" for i in range(n)]
    for i, day in enumerate(days):
        clean_runs.append(_write_decision(day, baseline_net=-10.0 - i, candidate_net=-9.0))
    result = losing_streak_and_stuck(settings)
    assert result["triggered"] is True
    assert result["losing_streak"] is True
    assert result["stuck"] is True


def test_trigger_does_not_fire_with_one_fewer_run_than_required(clean_runs):
    settings = load_settings()
    n = settings["evolution"]["losing_streak_runs"]
    days = [f"2099-02-{10 + i:02d}" for i in range(n - 1)]
    for day in days:
        clean_runs.append(_write_decision(day, baseline_net=-10.0, candidate_net=-9.0))
    result = losing_streak_and_stuck(settings)
    assert result["triggered"] is False
    assert "reason" in result


def test_trigger_does_not_fire_when_baseline_is_winning(clean_runs):
    settings = load_settings()
    n = settings["evolution"]["losing_streak_runs"]
    days = [f"2099-03-{10 + i:02d}" for i in range(n)]
    for i, day in enumerate(days):
        # one winning baseline in the middle of the streak breaks it
        baseline = 5.0 if i == n // 2 else -10.0
        clean_runs.append(_write_decision(day, baseline_net=baseline, candidate_net=-9.0))
    result = losing_streak_and_stuck(settings)
    assert result["losing_streak"] is False
    assert result["triggered"] is False


def test_trigger_does_not_fire_when_optimizer_is_still_improving(clean_runs):
    settings = load_settings()
    n = settings["evolution"]["losing_streak_runs"]
    days = [f"2099-04-{10 + i:02d}" for i in range(n)]
    for i, day in enumerate(days):
        # candidate net profit climbs meaningfully across the window: not stuck
        clean_runs.append(_write_decision(day, baseline_net=-10.0, candidate_net=-9.0 + i * 2.0))
    result = losing_streak_and_stuck(settings)
    assert result["stuck"] is False
    assert result["triggered"] is False


def test_trigger_boundary_exactly_at_stuck_epsilon(clean_runs):
    settings = load_settings()
    eps = settings["evolution"]["stuck_improvement_epsilon"]
    n = settings["evolution"]["losing_streak_runs"]
    days = [f"2099-05-{10 + i:02d}" for i in range(n)]
    first_best = -10.0
    last_best = first_best + first_best * -eps          # exactly eps relative improvement
    for i, day in enumerate(days):
        candidate = first_best if i == 0 else (last_best if i == n - 1 else -9.5)
        clean_runs.append(_write_decision(day, baseline_net=-10.0, candidate_net=candidate))
    result = losing_streak_and_stuck(settings)
    assert result["stuck"] is True   # "not more than epsilon" -> boundary counts as stuck


# ------------------------------------------------------------- cooldown
def test_cooldown_clear_with_no_prior_attempts(clean_runs):
    settings = load_settings()
    assert evolution_cooldown_clear(settings) is True


def test_cooldown_blocks_immediately_after_an_attempt(clean_runs):
    settings = load_settings()
    today = dt.date(2099, 6, 15)
    path = RUNS / f"evolution_{today.isoformat()}.json"
    path.write_text(json.dumps({"date": today.isoformat()}))
    clean_runs.append(path)
    assert evolution_cooldown_clear(settings, today=today) is False


def test_cooldown_clears_after_the_configured_quarters(clean_runs):
    settings = load_settings()
    quarters = settings["evolution"]["cooldown_quarters"]
    attempt_date = dt.date(2099, 1, 1)
    path = RUNS / f"evolution_{attempt_date.isoformat()}.json"
    path.write_text(json.dumps({"date": attempt_date.isoformat()}))
    clean_runs.append(path)
    later = dt.date(attempt_date.year, attempt_date.month + quarters * 3, attempt_date.day) \
        if attempt_date.month + quarters * 3 <= 12 \
        else dt.date(attempt_date.year + 1, attempt_date.month + quarters * 3 - 12, attempt_date.day)
    assert evolution_cooldown_clear(settings, today=later) is True


# --------------------------------------------------------- screen / refine
def _synthetic_candles(n_days=40, seed=5):
    rng = np.random.default_rng(seed)
    n = 24 * 4 * n_days
    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    steps = rng.normal(0, 3e-4, n) + 2e-5 * np.sin(np.arange(n) / 500)
    close = 1.10 + np.cumsum(steps)
    high = close + np.abs(rng.normal(0, 2e-4, n))
    low = close - np.abs(rng.normal(0, 2e-4, n))
    open_ = np.r_[close[0], close[:-1]]
    df15 = pd.DataFrame({"open": open_, "high": np.maximum.reduce([open_, close, high]),
                        "low": np.minimum.reduce([open_, close, low]), "close": close,
                        "volume": 1.0}, index=idx)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df1h = df15.resample("1h").agg(agg).dropna()
    return df15, df1h


def test_screen_ranks_all_genome_combinations_by_net_profit():
    settings = load_settings()
    df15, df1h = _synthetic_candles()
    params = json.loads(pathlib.Path("config/champion_zero.json").read_text())["params"]
    results = screen(df15, df1h, settings, {}, None, params)
    assert len(results) == 6   # 3 entry signals x 2 exit styles
    nets = [r["net_profit_pips"] for r in results]
    assert nets == sorted(nets, reverse=True)   # ranked, best first
    seen = {(r["genome"]["entry_signal"], r["genome"]["exit_style"]) for r in results}
    assert len(seen) == 6   # every combination appears exactly once


def test_refine_picks_the_best_of_top_k_by_training_net_profit():
    settings = load_settings()
    df15, df1h = _synthetic_candles(n_days=300)
    params = json.loads(pathlib.Path("config/champion_zero.json").read_text())["params"]
    screened = screen(df15, df1h, settings, {}, None, params)
    winner = refine(screened, df15, df1h, settings, {}, None, top_k=3, n_trials=5, timeout_s=30)
    assert winner["genome"] in [s["genome"] for s in screened[:3]]
    assert "net_profit_pips" in winner["train_metrics"]


def test_refine_skips_a_genome_that_cannot_reach_the_trade_floor():
    """A genome too sparse to hit min_oos_trades within the search budget
    (e.g. breakout on a short window) must be skipped, not fatal."""
    settings = load_settings()
    df15, df1h = _synthetic_candles(n_days=60)   # enough for ema_pullback, not for sparse breakout
    screened = [{"genome": {"entry_signal": "breakout", "exit_style": "atr_trail_half"},
                "net_profit_pips": 0.0, "n_trades": 0},
               {"genome": {"entry_signal": "ema_pullback", "exit_style": "atr_trail_half"},
                "net_profit_pips": 0.0, "n_trades": 0}]
    winner = refine(screened, df15, df1h, settings, {}, None, top_k=2, n_trials=5, timeout_s=30)
    assert winner["genome"]["entry_signal"] == "ema_pullback"   # the only one that can clear it


def test_refine_raises_when_every_candidate_is_unviable():
    settings = load_settings()
    df15, df1h = _synthetic_candles(n_days=1)   # far too short for anything to clear the floor
    screened = [{"genome": {"entry_signal": "breakout", "exit_style": "atr_trail_half"},
                "net_profit_pips": 0.0, "n_trades": 0}]
    with pytest.raises(RuntimeError):
        refine(screened, df15, df1h, settings, {}, None, top_k=1, n_trials=3, timeout_s=15)


# ----------------------------------------------------------- genome_id_for
def test_genome_id_for_reuses_baseline():
    assert genome_id_for({"entry_signal": "ema_pullback", "exit_style": "atr_trail_half"}) == "baseline"


def test_genome_id_for_mints_new_id_for_unseen_combination():
    gid = genome_id_for({"entry_signal": "breakout", "exit_style": "fixed_r_multiple"})
    assert gid == "breakout__fixed_r_multiple"


# -------------------------------------------------- phase0 is informational
def test_check_trigger_ignores_phase0_status(monkeypatch, clean_runs):
    """Phase 0 no longer gates evolution — only the losing-streak+stuck
    trigger and the cooldown do. A losing streak must fire the trigger
    regardless of whether phase0_passed() is True or False."""
    settings = load_settings()
    n = settings["evolution"]["losing_streak_runs"]
    for i in range(n):
        clean_runs.append(_write_decision(f"2099-10-{10 + i:02d}", -10.0 - i, -9.0 - i * 0.001))
    monkeypatch.setattr("src.evolve.phase0_passed", lambda: False)
    fires_when_failed = _check_trigger()
    monkeypatch.setattr("src.evolve.phase0_passed", lambda: True)
    fires_when_passed = _check_trigger()
    assert fires_when_failed == fires_when_passed is True


def test_run_evolution_reports_phase0_status_without_gating_on_it(monkeypatch):
    """When the trigger hasn't fired, run_evolution() takes the cheap
    not-triggered path (never touches real data) and its stub still surfaces
    phase0_passed() for visibility — but never as a blocking 'reason'. The
    trigger check itself is mocked so this can't accidentally read real repo
    state and fall through into a real (expensive) funnel run."""
    monkeypatch.setattr("src.evolve.phase0_passed", lambda: False)
    monkeypatch.setattr("src.evolve.losing_streak_and_stuck",
                        lambda settings: {"triggered": False, "reason": "not enough history"})
    result = run_evolution()
    assert result["attempted"] is False
    assert result["phase0_passed"] is False
    assert "Phase 0" not in result.get("reason", "")
