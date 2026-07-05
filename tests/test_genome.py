"""Genome module tests: each entry/exit choice tested in isolation on fixture
candles, plus registry validation. Filters not under test are relaxed (wide
RSI bounds, generous candle-body limit, all-day session) so each test
isolates the specific logic it targets, while still exercising the real
indicator functions from strategy.py.
"""
import pandas as pd
import pytest

from src.genome import ENTRY_SIGNALS, EXIT_STYLES, all_combinations, assemble, load_genome, validate_genome
from src.modules import entries, exits

FIXED = {"ema_fast": 2, "ema_mid": 3, "ema_slow": 5, "h1_ema_mid": 2, "h1_ema_slow": 3,
        "rsi_period": 3, "atr_period": 3,
        "session_start_hour_utc": 0, "session_end_hour_utc": 24}

PARAMS = {"rsi_buy_low": 0.0, "rsi_buy_high": 100.0, "rsi_sell_low": 0.0, "rsi_sell_high": 100.0,
         "atr_stop_mult": 1.5, "atr_target_mult": 1.5, "max_body_atr": 5.0, "entry_buffer_pips": 1.0}


def uptrend(n=60, start=1.1000, step=0.0006):
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")
    close = [start + i * step for i in range(n)]
    df = pd.DataFrame({"open": close, "high": [c + 0.0002 for c in close],
                       "low": [c - 0.0002 for c in close], "close": close}, index=idx)
    # one pullback bar dipping to the fast EMA area, without breaking the uptrend
    mid = n // 2
    df.iloc[mid, df.columns.get_loc("low")] -= 0.0009
    return df


def h1_from_15m(df15):
    return df15.resample("1h").agg({"open": "first", "high": "max",
                                    "low": "min", "close": "last"}).dropna()


# --------------------------------------------------------------- ema_pullback
def test_ema_pullback_fires_in_clear_uptrend_with_correct_formula():
    df15 = uptrend()
    df1h = h1_from_15m(df15)
    signals = entries.ema_pullback(df15, df1h, PARAMS, FIXED)
    assert len(signals) > 0
    for s in signals:
        assert s["direction"] == 1
        assert s["stop"] == pytest.approx(s["entry"] - PARAMS["atr_stop_mult"] * s["atr"])
        assert s["target"] == pytest.approx(s["entry"] + PARAMS["atr_target_mult"] * s["atr"])


def test_ema_pullback_no_signal_without_1h_trend_agreement():
    df15 = uptrend()
    flat_1h_idx = df15.resample("1h").agg({"close": "last"}).dropna().index
    flat_1h = pd.DataFrame({"open": 1.1000, "high": 1.1000, "low": 1.1000, "close": 1.1000},
                           index=flat_1h_idx)  # perfectly flat -> h1_trend is 0 everywhere
    signals = entries.ema_pullback(df15, flat_1h, PARAMS, FIXED)
    assert signals == []


def test_ema_pullback_session_filter_excludes_all_hours():
    df15 = uptrend()
    df1h = h1_from_15m(df15)
    closed_fixed = {**FIXED, "session_start_hour_utc": 23, "session_end_hour_utc": 23}
    signals = entries.ema_pullback(df15, df1h, PARAMS, closed_fixed)
    assert signals == []


def test_candle_quality_filter_blocks_wide_body_candle():
    n = 60
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")
    close = [1.1000 + i * 0.0006 for i in range(n)]
    open_ = [c - 0.0006 for c in close]   # a real body every bar, not zero-width
    df15 = pd.DataFrame({"open": open_, "high": [max(o, c) + 0.0002 for o, c in zip(open_, close)],
                        "low": [min(o, c) - 0.0002 for o, c in zip(open_, close)],
                        "close": close}, index=idx)
    df1h = h1_from_15m(df15)
    loose_signals = entries.ema_pullback(df15, df1h, PARAMS, FIXED)
    assert len(loose_signals) > 0   # sanity: this setup does fire with a generous body limit

    tight_params = {**PARAMS, "max_body_atr": 0.05}   # body (~0.0006) now far exceeds the limit
    signals = entries.ema_pullback(df15, df1h, tight_params, FIXED)
    assert signals == []


# ------------------------------------------------------------------ breakout
def test_breakout_fires_on_genuine_range_break_with_trend_agreement():
    n = 60
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")
    close = [1.1000] * (n - 1) + [1.1050]   # flat range, then a sharp breakout close
    df15 = pd.DataFrame({"open": close, "high": [c + 0.0003 for c in close],
                        "low": [c - 0.0003 for c in close], "close": close}, index=idx)
    df1h = df15.resample("1h").agg({"open": "first", "high": "max",
                                    "low": "min", "close": "last"}).dropna()
    df1h["close"] = df1h["close"] + pd.Series(range(len(df1h)), index=df1h.index) * 0.001  # uptrend
    signals = entries.breakout(df15, df1h, PARAMS, FIXED)
    assert any(s["direction"] == 1 for s in signals)


def test_breakout_no_signal_when_trend_disagrees():
    n = 60
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")
    close = [1.1000] * (n - 1) + [1.1050]
    df15 = pd.DataFrame({"open": close, "high": [c + 0.0003 for c in close],
                        "low": [c - 0.0003 for c in close], "close": close}, index=idx)
    flat_1h_idx = df15.resample("1h").agg({"close": "last"}).dropna().index
    flat_1h = pd.DataFrame({"open": 1.1000, "high": 1.1000, "low": 1.1000, "close": 1.1000},
                           index=flat_1h_idx)
    signals = entries.breakout(df15, flat_1h, PARAMS, FIXED)
    assert signals == []


# ------------------------------------------------------------ mean_reversion
# mean_reversion checks RSI against a single-sided extreme (r < rsi_sell_low
# for an oversold bounce, r > rsi_buy_high for an overbought fade) rather than
# a low<=r<=high band, and needs atr_stop_mult loose enough that a realistic
# sustained decline (not just one sharp bar, which inflates ATR alongside the
# gap) clears the "stretched from the mean" threshold.
MR_PARAMS = {**PARAMS, "rsi_sell_low": 90.0, "rsi_buy_high": 10.0, "atr_stop_mult": 0.5}


def _declining_then_bounce(n=25, slope=0.0015):
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")
    close = [1.1000 - i * slope for i in range(n - 1)]
    close.append(close[-1] + slope * 0.3)          # small bullish bounce on the final bar
    open_ = [1.1000 - (i - 1) * slope if i > 0 else 1.1000 for i in range(n - 1)] + [close[-2]]
    high = [max(o, c) + 0.00005 for o, c in zip(open_, close)]
    low = [min(o, c) - 0.00005 for o, c in zip(open_, close)]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_mean_reversion_fires_on_stretched_oversold_bounce():
    df15 = _declining_then_bounce()
    df1h = h1_from_15m(df15)
    signals = entries.mean_reversion(df15, df1h, MR_PARAMS, FIXED)
    assert any(s["direction"] == 1 for s in signals)
    for s in signals:
        assert s["stop"] == pytest.approx(s["entry"] - MR_PARAMS["atr_stop_mult"] * s["atr"])
        assert s["target"] == pytest.approx(s["entry"] + MR_PARAMS["atr_target_mult"] * s["atr"])


def test_mean_reversion_no_signal_without_stretch():
    n = 20
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")
    close = [1.1000] * n   # perfectly flat: never stretched from the mean
    df15 = pd.DataFrame({"open": close, "high": [c + 0.0002 for c in close],
                        "low": [c - 0.0002 for c in close], "close": close}, index=idx)
    df1h = df15.resample("1h").agg({"open": "first", "high": "max",
                                    "low": "min", "close": "last"}).dropna()
    signals = entries.mean_reversion(df15, df1h, MR_PARAMS, FIXED)
    assert signals == []


# --------------------------------------------------------------------- exits
SETTINGS = {"strategy_fixed": {"trail_lookback": 3}}


def exit_frame(bars):
    idx = pd.date_range("2026-01-05 08:00", periods=len(bars), freq="15min")
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)


def test_atr_trail_half_tp1_then_trail_matches_expected_legs():
    df = exit_frame([
        (1.0995, 1.0999, 1.0990, 1.0996),
        (1.0995, 1.1005, 1.0993, 1.1002),   # fill @ 1.1000
        (1.1002, 1.1022, 1.1001, 1.1020),   # TP1 hit -> half locked @ target, stop -> BE
        (1.1020, 1.1030, 1.1015, 1.1028),
        (1.1028, 1.1035, 1.1024, 1.1030),
        (1.1030, 1.1032, 1.1010, 1.1012),
        (1.1012, 1.1014, 1.1005, 1.1008),   # trail hit
    ])
    sig = {"direction": 1, "entry": 1.1000, "stop": 1.0980, "target": 1.1020}
    legs = exits.atr_trail_half(df, 1, sig, SETTINGS, {})
    assert len(legs) == 2
    assert legs[0][:3] == (1.1020, 0.5, False)   # TP1 leg
    assert legs[1][1] == pytest.approx(0.5) and legs[1][2] is True   # trail stop leg
    assert sum(f for _, f, _, _ in legs) == pytest.approx(1.0)


def test_fixed_r_multiple_target_first():
    df = exit_frame([
        (1.0995, 1.0999, 1.0990, 1.0996),
        (1.0995, 1.1005, 1.0993, 1.1002),   # fill @ 1.1000
        (1.1002, 1.1025, 1.1001, 1.1020),   # target hit, stop not touched
    ])
    sig = {"direction": 1, "entry": 1.1000, "stop": 1.0980, "target": 1.1020}
    legs = exits.fixed_r_multiple(df, 1, sig, SETTINGS, {})
    assert legs == [(1.1020, 1.0, False, 2)]


def test_fixed_r_multiple_worst_case_when_both_touched_same_bar():
    df = exit_frame([
        (1.0995, 1.0999, 1.0990, 1.0996),
        (1.0995, 1.1005, 1.0993, 1.1002),   # fill @ 1.1000
        (1.1000, 1.1025, 1.0975, 1.1010),   # both stop and target inside this bar
    ])
    sig = {"direction": 1, "entry": 1.1000, "stop": 1.0980, "target": 1.1020}
    legs = exits.fixed_r_multiple(df, 1, sig, SETTINGS, {})
    assert legs == [(1.0980, 1.0, True, 2)]   # no tick record -> worst case, stop first


def test_fixed_r_multiple_never_touched_returns_empty():
    df = exit_frame([(1.0995, 1.0999, 1.0990, 1.0996)] * 5)
    sig = {"direction": 1, "entry": 1.1000, "stop": 1.0980, "target": 1.1020}
    assert exits.fixed_r_multiple(df, 0, sig, SETTINGS, {}) == []


# ------------------------------------------------------------------ registry
def test_all_combinations_covers_every_pair_uniquely():
    combos = all_combinations()
    assert len(combos) == len(ENTRY_SIGNALS) * len(EXIT_STYLES)
    seen = {(c["entry_signal"], c["exit_style"]) for c in combos}
    assert len(seen) == len(combos)


def test_validate_genome_accepts_baseline():
    validate_genome({"entry_signal": "ema_pullback", "exit_style": "atr_trail_half"})


def test_validate_genome_rejects_unknown_entry():
    with pytest.raises(ValueError):
        validate_genome({"entry_signal": "not_a_real_module", "exit_style": "atr_trail_half"})


def test_validate_genome_rejects_unknown_exit():
    with pytest.raises(ValueError):
        validate_genome({"entry_signal": "ema_pullback", "exit_style": "not_a_real_module"})


def test_load_genome_baseline():
    g = load_genome("baseline")
    assert g["entry_signal"] == "ema_pullback"
    assert g["exit_style"] == "atr_trail_half"


def test_load_genome_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_genome("does_not_exist")


def test_assemble_defaults_to_baseline():
    entry_fn, exit_fn = assemble(None)
    assert entry_fn is ENTRY_SIGNALS["ema_pullback"]
    assert exit_fn is EXIT_STYLES["atr_trail_half"]


def test_assemble_explicit_genome():
    entry_fn, exit_fn = assemble({"entry_signal": "breakout", "exit_style": "fixed_r_multiple"})
    assert entry_fn is ENTRY_SIGNALS["breakout"]
    assert exit_fn is EXIT_STYLES["fixed_r_multiple"]
