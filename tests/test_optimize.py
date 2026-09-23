"""Walk-forward window-math tests.

split_windows() carves holdout | embargo | out-of-sample | embargo | training
purely from settings and the data's last timestamp. A silent date-arithmetic
error here (wrong order of subtraction, an offset applied to the wrong
boundary) would leak future data across train/test without ever raising an
exception — exactly the kind of bug the rest of the gate machinery cannot
catch, since the optimizer and evaluator both trust these boundaries blindly.

target_regime()'s stale-first precedence is likewise pure logic worth locking
down independently of regime.py's real ADX/ATR classifier.
"""
import pandas as pd

from src.optimize import split_windows, target_regime

WF = {"walk_forward": {"holdout_months": 6, "embargo_days": 5, "oos_days": 120}}


def _df(end: str) -> pd.DataFrame:
    return pd.DataFrame(index=pd.date_range("2020-01-01", end, freq="D"))


def test_split_windows_hand_computed_on_a_clean_month_boundary():
    """end=2026-07-01 makes the 6-month offset land on a bare month boundary
    (2026-01-01), so every subsequent day-count is verifiable by hand without
    any calendar ambiguity:
    holdout_start = 2026-01-01
    oos_end       = 2025-12-31                      (holdout_start - 1 day)
    oos_start     = 2025-12-31 - 120 days = 2025-09-02
                    (30 to Dec 1, +30 to Nov 1, +31 to Oct 1, +29 to Sep 2)
    train_end     = 2025-09-02 - 5 days   = 2025-08-28
    """
    w = split_windows(_df("2026-07-01"), WF)
    assert w["holdout_start"] == pd.Timestamp("2026-01-01")
    assert w["oos_end"] == pd.Timestamp("2025-12-31")
    assert w["oos_start"] == pd.Timestamp("2025-09-02")
    assert w["train_end"] == pd.Timestamp("2025-08-28")


def test_split_windows_boundaries_never_overlap():
    """Regardless of the specific settings, the five points must stay in
    strict chronological order — the property that actually protects against
    lookahead leakage, independent of any single hand-computed date."""
    for wf in (WF, {"walk_forward": {"holdout_months": 3, "embargo_days": 10, "oos_days": 60}},
              {"walk_forward": {"holdout_months": 1, "embargo_days": 1, "oos_days": 1}}):
        w = split_windows(_df("2026-09-22"), wf)
        assert w["train_end"] < w["oos_start"] < w["oos_end"] < w["holdout_start"] \
            < pd.Timestamp("2026-09-22")


def test_target_regime_prefers_first_stale_in_fixed_precedence_order(monkeypatch):
    """A stale set is revalidated before the prevailing regime is even
    consulted — and precedence follows the fixed (trending, ranging,
    high_volatility, low_volatility) order, not classifier output."""
    stale = {"trending": False, "ranging": True, "high_volatility": True, "low_volatility": False}
    monkeypatch.setattr("src.optimize.is_stale", lambda name, settings: stale[name])
    monkeypatch.setattr("src.optimize.classify", lambda df1h, settings: {"regime": "low_volatility"})
    assert target_regime(None, {}) == "ranging"


def test_target_regime_falls_back_to_classifier_when_nothing_stale(monkeypatch):
    monkeypatch.setattr("src.optimize.is_stale", lambda name, settings: False)
    monkeypatch.setattr("src.optimize.classify", lambda df1h, settings: {"regime": "trending"})
    assert target_regime(None, {}) == "trending"
