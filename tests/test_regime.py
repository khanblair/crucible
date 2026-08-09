"""Regime-classification tests: classify(), is_stale(), and validate_active().

Market regime classification determines which parameter set governs every
trade — a silent misclassification routes to the wrong config. Pure-arithmetic
functions; cheap and high-value to test.

classify() tests use monkeypatched ADX/ATR so each branch of the precedence
matrix is tested deterministically and is auditable by hand.
"""
import datetime as dt
import json
import pathlib

import pandas as pd
import pytest

from src.regime import classify, is_stale, validate_active

SETTINGS = {
    "regime": {
        "adx_period": 14,
        "adx_trend": 25,
        "adx_neutral_low": 20,
        "atr_high_percentile": 80,
        "atr_low_percentile": 20,
        "atr_window_days": 90,
        "staleness_days": 90,
    }
}


# ----------------------------------------------------------------- classify()
def _make_1h(n_bars: int = 100) -> pd.DataFrame:
    """Minimal 1h frame — indicator values are monkeypatched per test."""
    idx = pd.date_range("2025-01-01", periods=n_bars, freq="1h")
    return pd.DataFrame(
        {"open": 1.1000, "high": 1.1002, "low": 1.0998, "close": 1.1000},
        index=idx,
    )


def test_classify_high_volatility_outranks_everything(monkeypatch):
    """Rule 1: ATR > high_percentile outranks even a strong trend."""
    monkeypatch.setattr("src.regime.adx", lambda df, period: pd.Series([40.0]))
    # Current ATR very high relative to the window: early bars tiny, late bars huge
    atr_vals = [0.0001] * 2000 + [0.0050] * 160
    monkeypatch.setattr("src.regime.atr", lambda df, period: pd.Series(atr_vals, dtype=float))
    result = classify(_make_1h(2160), SETTINGS)
    assert result["regime"] == "high_volatility"
    assert "high volatility" in result["rule"].lower()


def test_classify_strong_trend_with_normal_volatility_is_trending(monkeypatch):
    """Rule 2: ADX > trend threshold, ATR not in high percentile."""
    monkeypatch.setattr("src.regime.adx", lambda df, period: pd.Series([30.0]))
    monkeypatch.setattr("src.regime.atr", lambda df, period: pd.Series([0.00030] * 3000, dtype=float))
    result = classify(_make_1h(), SETTINGS)
    assert result["regime"] == "trending"
    assert "quiet trend" in result["rule"].lower()


def test_classify_low_volatility_no_trend(monkeypatch):
    """Rule 3: ATR < low_percentile in the absence of trend."""
    monkeypatch.setattr("src.regime.adx", lambda df, period: pd.Series([15.0]))
    atr_vals = [0.0010] * 2000 + [0.0001] * 160
    monkeypatch.setattr("src.regime.atr", lambda df, period: pd.Series(atr_vals, dtype=float))
    result = classify(_make_1h(2160), SETTINGS)
    assert result["regime"] == "low_volatility"
    assert "low volatility" in result["rule"].lower()


def test_classify_neutral_defaults_to_ranging(monkeypatch):
    """Rule 3 default: neutral ADX with mid-range volatility -> ranging."""
    monkeypatch.setattr("src.regime.adx", lambda df, period: pd.Series([22.0]))
    # Rising then flat: current ATR near the median of its own window
    atr_vals = [0.0001] * 1000 + [0.0003] * 600 + [0.0005] * 560
    monkeypatch.setattr("src.regime.atr", lambda df, period: pd.Series(atr_vals, dtype=float))
    result = classify(_make_1h(2160), SETTINGS)
    assert result["regime"] == "ranging"


def test_classify_returns_all_required_fields(monkeypatch):
    monkeypatch.setattr("src.regime.adx", lambda df, period: pd.Series([30.0]))
    monkeypatch.setattr("src.regime.atr", lambda df, period: pd.Series([0.0003] * 3000, dtype=float))
    result = classify(_make_1h(), SETTINGS)
    for field in ("regime", "rule", "adx", "atr", "atr_percentile"):
        assert field in result, f"missing field '{field}'"
    assert isinstance(result["adx"], float)
    assert isinstance(result["atr"], float)
    assert isinstance(result["atr_percentile"], float)
    assert 0 <= result["atr_percentile"] <= 100


# ---------------------------------------------------------------- is_stale()
@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Isolate regime config reads to tmp_path with all needed dirs."""
    monkeypatch.setattr("src.regime.ROOT", tmp_path)
    (tmp_path / "config" / "regimes").mkdir(parents=True)
    (tmp_path / "config" / "genomes").mkdir(parents=True)
    return tmp_path


def _write_regime(root: pathlib.Path, name: str, last_validated: str) -> None:
    (root / "config" / "regimes" / f"{name}.json").write_text(json.dumps({
        "name": name, "genome": "baseline", "last_validated": last_validated,
        "params": {"rsi_buy_low": 45.0, "rsi_buy_high": 65.0, "rsi_sell_low": 35.0,
                   "rsi_sell_high": 55.0, "atr_stop_mult": 1.5, "atr_target_mult": 1.5,
                   "max_body_atr": 0.5, "entry_buffer_pips": 2.0},
    }))


def _write_genome(root: pathlib.Path, gid: str = "baseline") -> None:
    (root / "config" / "genomes" / f"{gid}.json").write_text(json.dumps({
        "id": gid, "entry_signal": "ema_pullback", "exit_style": "atr_trail_half",
        "created": "2026-01-01",
    }))


def _write_active(root: pathlib.Path, params_file: str) -> None:
    (root / "config" / "active.json").write_text(json.dumps({
        "regime": "trending",
        "params_file": params_file,
        "fallback_champion_zero": False,
        "updated": dt.date.today().isoformat(),
    }))


def test_is_stale_false_when_recently_validated(isolated_config):
    _write_regime(isolated_config, "trending", dt.date.today().isoformat())
    assert is_stale("trending", SETTINGS, today=dt.date.today()) is False


def test_is_stale_true_when_past_staleness_window(isolated_config):
    _write_regime(isolated_config, "trending",
                  (dt.date.today() - dt.timedelta(days=92)).isoformat())
    assert is_stale("trending", SETTINGS, today=dt.date.today()) is True


def test_is_stale_boundary_exactly_at_staleness_days(isolated_config):
    """is_stale uses a strict > check: (today - validated).days > staleness_days.
    Exactly staleness_days ago is NOT stale."""
    days = SETTINGS["regime"]["staleness_days"]
    _write_regime(isolated_config, "trending",
                  (dt.date.today() - dt.timedelta(days=days)).isoformat())
    assert is_stale("trending", SETTINGS, today=dt.date.today()) is False


def test_is_stale_boundary_one_day_before_window(isolated_config):
    days = SETTINGS["regime"]["staleness_days"]
    _write_regime(isolated_config, "trending",
                  (dt.date.today() - dt.timedelta(days=days - 1)).isoformat())
    assert is_stale("trending", SETTINGS, today=dt.date.today()) is False


# ---------------------------------------------------------- validate_active()
def test_validate_active_happy_path(isolated_config):
    _write_genome(isolated_config, "baseline")
    _write_regime(isolated_config, "trending", dt.date.today().isoformat())
    _write_active(isolated_config, "config/regimes/trending.json")
    active = validate_active()
    assert active["regime"] == "trending"


def test_validate_active_raises_on_missing_params_file(isolated_config):
    _write_active(isolated_config, "config/regimes/nonexistent.json")
    with pytest.raises(FileNotFoundError, match="points to missing"):
        validate_active()


def test_validate_active_raises_on_broken_genome_reference(isolated_config):
    (isolated_config / "config" / "regimes" / "trending.json").write_text(json.dumps({
        "name": "trending", "genome": "impossible_genome",
        "last_validated": dt.date.today().isoformat(),
        "params": {"rsi_buy_low": 45.0, "rsi_buy_high": 65.0, "rsi_sell_low": 35.0,
                   "rsi_sell_high": 55.0, "atr_stop_mult": 1.5, "atr_target_mult": 1.5,
                   "max_body_atr": 0.5, "entry_buffer_pips": 2.0},
    }))
    _write_active(isolated_config, "config/regimes/trending.json")
    with pytest.raises(FileNotFoundError, match="unknown genome"):
        validate_active()


def test_validate_active_accepts_regime_without_genome_field(isolated_config):
    (isolated_config / "config" / "regimes" / "trending.json").write_text(json.dumps({
        "name": "trending",
        "last_validated": dt.date.today().isoformat(),
        "params": {"rsi_buy_low": 45.0, "rsi_buy_high": 65.0, "rsi_sell_low": 35.0,
                   "rsi_sell_high": 55.0, "atr_stop_mult": 1.5, "atr_target_mult": 1.5,
                   "max_body_atr": 0.5, "entry_buffer_pips": 2.0},
    }))
    _write_active(isolated_config, "config/regimes/trending.json")
    active = validate_active()
    assert active["regime"] == "trending"


def test_validate_active_raises_on_invalid_params(isolated_config):
    (isolated_config / "config" / "regimes" / "trending.json").write_text(json.dumps({
        "name": "trending", "genome": "baseline",
        "last_validated": dt.date.today().isoformat(),
        "params": {"not_a_real_param": 1.0},
    }))
    _write_active(isolated_config, "config/regimes/trending.json")
    with pytest.raises(ValueError, match="parameter file invalid"):
        validate_active()
