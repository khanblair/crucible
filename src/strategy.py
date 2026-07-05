"""Fixed strategy logic: indicators and filters shared by every genome.

Human-edited only. The automated workflow never modifies this file.
Entry-signal and exit-style logic that genome evolution can select between
lives in src/modules/ instead — this file holds only what never varies:
indicators, the 1h trend filter, intrabar-touch resolution, and parameter
schema validation. Only the numeric boundaries in `params` are optimizable;
which module runs is a `genome` choice, gated separately (see src/genome.py).
"""
import pandas as pd

PIP = 0.0001


# ---------------------------------------------------------------- indicators
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, 1e-12)
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    trn = atr(df, period).replace(0.0, 1e-12)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / trn
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / trn
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-12)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def h1_trend(df1h: pd.DataFrame, fixed: dict) -> pd.Series:
    """+1 uptrend, -1 downtrend, 0 no clear direction, per 1-hour bar.
    Shared by every entry-signal module — the 1h trend filter never varies."""
    mid = ema(df1h["close"], fixed["h1_ema_mid"])
    slow = ema(df1h["close"], fixed["h1_ema_slow"])
    trend = pd.Series(0, index=df1h.index)
    trend[(df1h["close"] > mid) & (mid > slow)] = 1
    trend[(df1h["close"] < mid) & (mid < slow)] = -1
    return trend


def session_ok(t, fixed: dict) -> bool:
    """London/New York session filter — shared by every entry-signal module."""
    return fixed["session_start_hour_utc"] <= t.hour < fixed["session_end_hour_utc"]


def candle_quality_ok(o: float, c: float, bar_atr: float, params: dict) -> bool:
    """Small body relative to volatility — shared by every entry-signal module."""
    return bar_atr > 0 and abs(c - o) <= params["max_body_atr"] * bar_atr


def resolve_first_touch(bar_time, direction: int, intrabar: dict) -> str:
    """Which side of a bar was touched first. No tick record => worst case:
    the stop-loss is assumed hit first. Never the favorable outcome.
    Shared by every exit-style module that needs same-bar stop/target ordering."""
    side = intrabar.get(bar_time)
    if side is None:
        return "stop"
    if direction == 1:  # long: stop below (low), target above (high)
        return "stop" if side == "low_first" else "target"
    return "stop" if side == "high_first" else "target"


REQUIRED_PARAM_KEYS = ("rsi_buy_low", "rsi_buy_high", "rsi_sell_low", "rsi_sell_high",
                       "atr_stop_mult", "atr_target_mult", "max_body_atr", "entry_buffer_pips")


def validate_params(params: dict) -> None:
    """Schema check used by every run before anything else executes."""
    for key in REQUIRED_PARAM_KEYS:
        if key not in params or not isinstance(params[key], (int, float)):
            raise ValueError(f"parameter file invalid: missing/non-numeric '{key}'")
