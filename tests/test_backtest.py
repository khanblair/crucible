"""Harness tests: fixture candles with hand-computed outcomes.

A bug in the harness corrupts the optimizer, the gate and the forward log
identically — which is exactly why it cannot go untested. Settings are
spelled out here so every expected value can be recomputed by hand.
"""
import pandas as pd
import pytest

from src.backtest import compute_metrics, phase0_passed, simulate_trade

SETTINGS = {
    "costs": {"spread_pips": 1.0, "slippage_pips": 0.2,
              "swap_long_pips_per_day": -0.6, "swap_short_pips_per_day": 0.2,
              "rollover_hour_utc": 21, "triple_swap_weekday": 2},
    "strategy_fixed": {"entry_valid_bars": 4, "trail_lookback": 3},
    "backtest": {"start_equity_pips": 1000.0},
}


def frame(start: str, bars: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(bars), freq="15min")
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)


def long_sig(t) -> dict:
    return {"time": t, "direction": 1, "entry": 1.1000, "stop": 1.0980, "target": 1.1020}


def test_costs_applied_full_winner_with_trailing_exit():
    """Fill 1.1000; TP1 +20 pips on half; trail exits the rest at 1.1010 (+10 on half).
    Gross 15.0 - spread 1.0 - slippage 0.2*(entry 1.0 + stop-exit 0.5) = net 13.7."""
    df = frame("2026-01-05 08:00", [
        (1.0995, 1.0999, 1.0990, 1.0996),   # signal bar
        (1.0995, 1.1005, 1.0993, 1.1002),   # fill at 1.1000
        (1.1002, 1.1022, 1.1001, 1.1020),   # TP1 hit, stop -> breakeven
        (1.1020, 1.1030, 1.1015, 1.1028),
        (1.1028, 1.1035, 1.1024, 1.1030),
        (1.1030, 1.1032, 1.1010, 1.1012),
        (1.1012, 1.1014, 1.1005, 1.1008),   # trail (min low of prev 3 = 1.1010) hit
    ])
    t = simulate_trade(df, 0, long_sig(df.index[0]), SETTINGS)
    assert t["gross_pips"] == pytest.approx(15.0)
    assert t["slippage_pips"] == pytest.approx(0.3)
    assert t["swap_pips"] == 0.0
    assert t["pnl_pips"] == pytest.approx(13.7)


def test_intrabar_worst_case_fallback_when_no_tick_record():
    """Bar 2 touches BOTH stop and target. With no tick record the engine must
    assume the stop was hit first: full loss -20 - 1.0 - 0.4 = -21.4."""
    df = frame("2026-01-05 08:00", [
        (1.0995, 1.0999, 1.0990, 1.0996),
        (1.0995, 1.1005, 1.0993, 1.1002),   # fill at 1.1000
        (1.1000, 1.1025, 1.0975, 1.1010),   # stop AND target inside this bar
    ])
    t = simulate_trade(df, 0, long_sig(df.index[0]), SETTINGS, intrabar={})
    assert t["pnl_pips"] == pytest.approx(-21.4)


def test_intrabar_table_resolves_favorable_ordering():
    """Same bar, but the tick record says the high came first: TP1 fills half
    (+20), stop moves to breakeven and the low then stops the rest at entry.
    Net = 10.0 - 1.0 - 0.3 = 8.7."""
    df = frame("2026-01-05 08:00", [
        (1.0995, 1.0999, 1.0990, 1.0996),
        (1.0995, 1.1005, 1.0993, 1.1002),
        (1.1000, 1.1025, 1.0975, 1.1010),
    ])
    t = simulate_trade(df, 0, long_sig(df.index[0]), SETTINGS,
                       intrabar={df.index[2]: "high_first"})
    assert t["pnl_pips"] == pytest.approx(8.7)


def test_intrabar_low_first_is_stop_for_longs():
    df = frame("2026-01-05 08:00", [
        (1.0995, 1.0999, 1.0990, 1.0996),
        (1.0995, 1.1005, 1.0993, 1.1002),
        (1.1000, 1.1025, 1.0975, 1.1010),
    ])
    t = simulate_trade(df, 0, long_sig(df.index[0]), SETTINGS,
                       intrabar={df.index[2]: "low_first"})
    assert t["pnl_pips"] == pytest.approx(-21.4)


def _overnight_frame(start: str, n_flat: int, last_bar: tuple) -> pd.DataFrame:
    bars = [(1.0999, 1.1002, 1.0998, 1.1000),   # signal bar
            (1.0999, 1.1002, 1.0998, 1.1000)]   # fill bar (touches 1.1000)
    bars += [(1.1000, 1.1004, 1.0996, 1.1000)] * n_flat
    bars.append(last_bar)
    return frame(start, bars)


def test_triple_swap_on_wednesday_rollover_long():
    """Long filled Wed 18:15, held across the Wed 21:00 rollover (triple:
    3 x -0.6 = -1.8), stopped Thu morning: -50 - 1.0 - 0.4 - 1.8 = -53.2."""
    # 2026-01-07 is a Wednesday; flat bars 18:30 -> Thu 08:45 = 58 bars
    df = _overnight_frame("2026-01-07 18:00", 58, (1.0996, 1.0998, 1.0948, 1.0960))
    assert df.index[-1] == pd.Timestamp("2026-01-08 09:00")
    sig = {"time": df.index[0], "direction": 1,
           "entry": 1.1000, "stop": 1.0950, "target": 1.1050}
    t = simulate_trade(df, 0, sig, SETTINGS)
    assert t["swap_pips"] == pytest.approx(-1.8)
    assert t["pnl_pips"] == pytest.approx(-53.2)


def test_swap_scales_by_fraction_still_open_after_tp1():
    """TP1 locks half the position before any rollover is crossed; three
    rollovers (Mon, Tue, Wed-triple) are then crossed while only the half
    remains open, before it stops out. Swap must scale to that 0.5 fraction:
    0.5 * (-0.6 - 0.6 - 1.8) = -1.5, not the full-position -3.0."""
    n = 400
    idx = pd.date_range("2026-01-05 08:00", periods=n, freq="15min")  # Monday
    df = pd.DataFrame(index=idx, columns=["open", "high", "low", "close"], dtype=float)
    for i in range(n):
        base = 1.1010 + i * 0.000005   # gentle uptrend so the trail never catches up early
        df.iloc[i] = [base, base + 0.0004, base - 0.0004, base]
    df.iloc[0] = [1.0995, 1.0999, 1.0990, 1.0996]
    df.iloc[1] = [1.0995, 1.1005, 1.0993, 1.1002]   # fill @ 1.1000
    df.iloc[2] = [1.1002, 1.1022, 1.1001, 1.1020]   # TP1 hit -> half locked, stop -> BE
    wed = df.index[(df.index.weekday == 2) & (df.index.hour == 21) & (df.index.minute == 0)][0]
    drop = df.index.get_loc(wed) + 6
    b = df.iloc[drop]
    df.iloc[drop] = [b["open"], b["high"], b["low"] - 0.01, b["close"]]  # force the remainder out

    t = simulate_trade(df, 0, long_sig(df.index[0]), SETTINGS)
    assert t["swap_pips"] == pytest.approx(-1.5)


def test_single_swap_credit_short_non_wednesday():
    """Short held across Monday's rollover earns +0.2; stopped Tue morning:
    -50 - 1.0 - 0.4 + 0.2 = -51.2."""
    # 2026-01-05 is a Monday; flat bars 18:30 -> Tue 08:45 = 58 bars
    df = _overnight_frame("2026-01-05 18:00", 58, (1.1002, 1.1052, 1.1000, 1.1040))
    sig = {"time": df.index[0], "direction": -1,
           "entry": 1.1000, "stop": 1.1050, "target": 1.0950}
    t = simulate_trade(df, 0, sig, SETTINGS)
    assert t["swap_pips"] == pytest.approx(0.2)
    assert t["pnl_pips"] == pytest.approx(-51.2)


def test_entry_never_filled_returns_none():
    df = frame("2026-01-05 08:00", [(1.0990, 1.0995, 1.0985, 1.0990)] * 6)
    assert simulate_trade(df, 0, long_sig(df.index[0]), SETTINGS) is None


def test_metrics_hand_computed():
    trades = [{"pnl_pips": 10.0}, {"pnl_pips": -5.0}, {"pnl_pips": 15.0}]
    m = compute_metrics(trades, SETTINGS)
    assert m["n_trades"] == 3
    assert m["net_profit_pips"] == pytest.approx(20.0)
    assert m["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["expectancy_pips"] == pytest.approx(20 / 3, abs=1e-4)
    assert m["profit_factor"] == pytest.approx(5.0)
    # equity 1000 -> 1010 -> 1005 -> 1020; worst dip 5 from peak 1010
    assert m["max_drawdown"] == pytest.approx(5 / 1010, abs=1e-4)


# ------------------------------------------------------------ phase0_passed
def test_phase0_passed_true_on_pass_verdict(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "phase0_report.md").write_text("**Verdict: PASS**\n")
    monkeypatch.setattr("src.backtest.ROOT", tmp_path)
    assert phase0_passed() is True


def test_phase0_passed_false_on_fail_verdict(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "phase0_report.md").write_text("**Verdict: FAIL**\n")
    monkeypatch.setattr("src.backtest.ROOT", tmp_path)
    assert phase0_passed() is False


def test_phase0_passed_false_when_report_missing(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr("src.backtest.ROOT", tmp_path)
    assert phase0_passed() is False


# --------------------------------------------------- numpy dtype boundary
def test_metrics_and_trades_are_json_serializable_on_real_pandas_data():
    """Real production bug: prices come from pandas .iloc[] indexing, which
    yields numpy.float64/numpy.bool_, not native Python types. Hand-built
    float-literal fixtures (used everywhere else in this file) never exercise
    that path, so this test deliberately runs the harness on a real
    DataFrame — exactly what broke json.dumps() in the first live
    optimize.yml run once Phase 0 stopped gating it."""
    import json as _json

    import numpy as np

    from src.backtest import run_backtest

    rng = np.random.default_rng(3)
    n = 24 * 4 * 20
    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    close = 1.10 + np.cumsum(rng.normal(0, 3e-4, n))
    high = close + np.abs(rng.normal(0, 2e-4, n))
    low = close - np.abs(rng.normal(0, 2e-4, n))
    open_ = np.r_[close[0], close[:-1]]
    df15 = pd.DataFrame({"open": open_, "high": np.maximum.reduce([open_, close, high]),
                        "low": np.minimum.reduce([open_, close, low]), "close": close,
                        "volume": 1.0}, index=idx)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df1h = df15.resample("1h").agg(agg).dropna()
    real_settings = {**SETTINGS, "strategy_fixed": {"ema_fast": 8, "ema_mid": 21, "ema_slow": 50,
                     "h1_ema_mid": 50, "h1_ema_slow": 200, "rsi_period": 14, "atr_period": 14,
                     "session_start_hour_utc": 0, "session_end_hour_utc": 24,
                     "entry_valid_bars": 4, "trail_lookback": 3}}
    params = {"rsi_buy_low": 45.0, "rsi_buy_high": 65.0, "rsi_sell_low": 35.0,
             "rsi_sell_high": 55.0, "atr_stop_mult": 1.5, "atr_target_mult": 1.5,
             "max_body_atr": 0.5, "entry_buffer_pips": 2.0}

    result = run_backtest(df15, df1h, params, real_settings)
    assert result["metrics"]["n_trades"] > 0, "fixture must produce at least one real trade"
    # `type(x) is float`, not isinstance: numpy.float64 SUBCLASSES float (and so
    # passes isinstance/json.dumps just fine) — type() is the only check strict
    # enough to catch it. numpy.bool_ is the one that actually breaks json.dumps,
    # but a leaked float64 here is exactly the kind of value whose *comparisons*
    # (in evaluate.py's gates) produce the numpy.bool_ that does break it.
    for trade in result["trades"]:
        for key in ("entry", "gross_pips", "slippage_pips", "swap_pips", "pnl_pips"):
            assert type(trade[key]) is float, f"{key} is {type(trade[key])}, not native float"
    for key in ("net_profit_pips", "expectancy_pips", "win_rate", "profit_factor", "max_drawdown"):
        assert type(result["metrics"][key]) is float, \
            f"{key} is {type(result['metrics'][key])}, not native float"
    _json.dumps(result["metrics"])   # must not raise TypeError: Object of type ... not JSON serializable
    _json.dumps(result["trades"])
