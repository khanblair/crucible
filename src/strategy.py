"""Fixed strategy logic: indicators, signals, entries, exits.

Human-edited only. The automated workflow never modifies this file.
Only the numeric boundaries passed in via `params` are optimizable.
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


# ------------------------------------------------------------------- signals
def h1_trend(df1h: pd.DataFrame, fixed: dict) -> pd.Series:
    """+1 uptrend, -1 downtrend, 0 no clear direction, per 1-hour bar."""
    mid = ema(df1h["close"], fixed["h1_ema_mid"])
    slow = ema(df1h["close"], fixed["h1_ema_slow"])
    trend = pd.Series(0, index=df1h.index)
    trend[(df1h["close"] > mid) & (mid > slow)] = 1
    trend[(df1h["close"] < mid) & (mid < slow)] = -1
    return trend


def generate_signals(df15: pd.DataFrame, df1h: pd.DataFrame,
                     params: dict, fixed: dict) -> list[dict]:
    """Scan 15m bars for entries confirmed by the 1h trend.

    Returns a list of signal dicts: time, direction, entry, stop, target, atr.
    """
    fast = ema(df15["close"], fixed["ema_fast"])
    mid = ema(df15["close"], fixed["ema_mid"])
    slow = ema(df15["close"], fixed["ema_slow"])
    r = rsi(df15["close"], fixed["rsi_period"])
    a = atr(df15, fixed["atr_period"])
    # trend of the last completed 1h bar at or before each 15m bar
    trend = h1_trend(df1h, fixed).reindex(df15.index, method="ffill").fillna(0)

    buf = params["entry_buffer_pips"] * PIP
    signals = []
    warmup = max(fixed["h1_ema_slow"] // 4, fixed["ema_slow"], fixed["atr_period"]) + 1
    for i in range(warmup, len(df15)):
        t = df15.index[i]
        if not (fixed["session_start_hour_utc"] <= t.hour < fixed["session_end_hour_utc"]):
            continue
        o, h, l, c = df15.iloc[i][["open", "high", "low", "close"]]
        bar_atr = a.iloc[i]
        if bar_atr <= 0 or abs(c - o) > params["max_body_atr"] * bar_atr:
            continue  # candle quality: small body relative to volatility
        aligned_up = fast.iloc[i] > mid.iloc[i] > slow.iloc[i]
        aligned_dn = fast.iloc[i] < mid.iloc[i] < slow.iloc[i]
        if aligned_up and trend.iloc[i] == 1 and l <= fast.iloc[i] and c > slow.iloc[i] \
                and params["rsi_buy_low"] <= r.iloc[i] <= params["rsi_buy_high"]:
            entry = h + buf
            signals.append({"time": t, "direction": 1, "entry": entry,
                            "stop": entry - params["atr_stop_mult"] * bar_atr,
                            "target": entry + params["atr_target_mult"] * bar_atr,
                            "atr": bar_atr})
        elif aligned_dn and trend.iloc[i] == -1 and h >= fast.iloc[i] and c < slow.iloc[i] \
                and params["rsi_sell_low"] <= r.iloc[i] <= params["rsi_sell_high"]:
            entry = l - buf
            signals.append({"time": t, "direction": -1, "entry": entry,
                            "stop": entry + params["atr_stop_mult"] * bar_atr,
                            "target": entry - params["atr_target_mult"] * bar_atr,
                            "atr": bar_atr})
    return signals


REQUIRED_PARAM_KEYS = ("rsi_buy_low", "rsi_buy_high", "rsi_sell_low", "rsi_sell_high",
                       "atr_stop_mult", "atr_target_mult", "max_body_atr", "entry_buffer_pips")


def validate_params(params: dict) -> None:
    """Schema check used by every run before anything else executes."""
    for key in REQUIRED_PARAM_KEYS:
        if key not in params or not isinstance(params[key], (int, float)):
            raise ValueError(f"parameter file invalid: missing/non-numeric '{key}'")
