"""Fixed-logic tests: the parameter schema gate and the shared filters every
entry-signal module depends on.

validate_params() runs before anything else executes each cycle — a config
file that silently drifts out of schema (a dropped key, a string where a
number belongs) must fail loudly here, not surface later as a cryptic
KeyError/TypeError deep inside the harness.
"""
import pytest

from src.strategy import REQUIRED_PARAM_KEYS, candle_quality_ok, session_ok, validate_params

VALID_PARAMS = {
    "rsi_buy_low": 45.0, "rsi_buy_high": 65.0, "rsi_sell_low": 35.0, "rsi_sell_high": 55.0,
    "atr_stop_mult": 1.5, "atr_target_mult": 1.5, "max_body_atr": 0.5, "entry_buffer_pips": 2.0,
}


def test_validate_params_accepts_a_complete_numeric_set():
    validate_params(VALID_PARAMS)  # must not raise


def test_validate_params_rejects_missing_key():
    incomplete = {k: v for k, v in VALID_PARAMS.items() if k != "atr_stop_mult"}
    with pytest.raises(ValueError, match="atr_stop_mult"):
        validate_params(incomplete)


def test_validate_params_rejects_non_numeric_value():
    bad = {**VALID_PARAMS, "rsi_buy_low": "45.0"}
    with pytest.raises(ValueError, match="rsi_buy_low"):
        validate_params(bad)


def test_validate_params_checks_every_required_key():
    # every key in the schema is independently load-bearing, not just the first
    for key in REQUIRED_PARAM_KEYS:
        incomplete = {k: v for k, v in VALID_PARAMS.items() if k != key}
        with pytest.raises(ValueError):
            validate_params(incomplete)


FIXED = {"session_start_hour_utc": 7, "session_end_hour_utc": 16}


def test_session_ok_boundaries():
    import pandas as pd
    assert session_ok(pd.Timestamp("2026-01-05 07:00"), FIXED)   # start inclusive
    assert session_ok(pd.Timestamp("2026-01-05 15:59"), FIXED)
    assert not session_ok(pd.Timestamp("2026-01-05 06:59"), FIXED)
    assert not session_ok(pd.Timestamp("2026-01-05 16:00"), FIXED)  # end exclusive


def test_candle_quality_ok_boundary():
    params = {"max_body_atr": 0.5}
    # body exactly half the bar's ATR: passes (<=)
    assert candle_quality_ok(o=1.1000, c=1.1005, bar_atr=0.0010, params=params)
    # body just over half: fails
    assert not candle_quality_ok(o=1.1000, c=1.10051, bar_atr=0.0010, params=params)
    # zero ATR (no volatility reading yet) is never tradeable
    assert not candle_quality_ok(o=1.1000, c=1.1000, bar_atr=0.0, params=params)
