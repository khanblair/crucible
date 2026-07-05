"""Entry-signal modules: the genome-selectable choices for 'when does a trade
start'. Every module shares the same fixed filters (session, candle quality,
1h trend) and the same params dict shape (rsi_buy_low/high, rsi_sell_low/high,
atr_stop_mult, atr_target_mult, max_body_atr, entry_buffer_pips) so genome
evolution never changes the search space, only which logic decides entries.

Each module returns a list of signal dicts: time, direction, entry, stop,
target, atr — the shape backtest.py's fill-detection and cost model expect,
regardless of which entry module produced them.
"""
from src.strategy import PIP, atr as atr_ind, candle_quality_ok, ema, h1_trend, rsi, session_ok


def ema_pullback(df15, df1h, params: dict, fixed: dict) -> list[dict]:
    """Today's baseline logic: three aligned EMAs signal momentum, a pullback
    to the fast EMA signals a pause, RSI confirms healthy (not extreme)
    momentum, and the 1h trend must agree."""
    fast = ema(df15["close"], fixed["ema_fast"])
    mid = ema(df15["close"], fixed["ema_mid"])
    slow = ema(df15["close"], fixed["ema_slow"])
    r = rsi(df15["close"], fixed["rsi_period"])
    a = atr_ind(df15, fixed["atr_period"])
    trend = h1_trend(df1h, fixed).reindex(df15.index, method="ffill").fillna(0)

    buf = params["entry_buffer_pips"] * PIP
    signals = []
    warmup = max(fixed["h1_ema_slow"] // 4, fixed["ema_slow"], fixed["atr_period"]) + 1
    for i in range(warmup, len(df15)):
        t = df15.index[i]
        if not session_ok(t, fixed):
            continue
        o, h, l, c = df15.iloc[i][["open", "high", "low", "close"]]
        bar_atr = a.iloc[i]
        if not candle_quality_ok(o, c, bar_atr, params):
            continue
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


BREAKOUT_LOOKBACK = 20  # bars; a structural constant of this entry style, not a tunable param


def breakout(df15, df1h, params: dict, fixed: dict) -> list[dict]:
    """A close beyond the recent range, confirmed by the 1h trend and healthy
    (not extreme) RSI momentum — trades continuation instead of a pullback."""
    r = rsi(df15["close"], fixed["rsi_period"])
    a = atr_ind(df15, fixed["atr_period"])
    trend = h1_trend(df1h, fixed).reindex(df15.index, method="ffill").fillna(0)
    recent_high = df15["high"].shift().rolling(BREAKOUT_LOOKBACK).max()
    recent_low = df15["low"].shift().rolling(BREAKOUT_LOOKBACK).min()

    buf = params["entry_buffer_pips"] * PIP
    signals = []
    warmup = max(fixed["h1_ema_slow"] // 4, BREAKOUT_LOOKBACK, fixed["atr_period"]) + 1
    for i in range(warmup, len(df15)):
        t = df15.index[i]
        if not session_ok(t, fixed):
            continue
        o, h, l, c = df15.iloc[i][["open", "high", "low", "close"]]
        bar_atr = a.iloc[i]
        if not candle_quality_ok(o, c, bar_atr, params):
            continue
        if c > recent_high.iloc[i] and trend.iloc[i] == 1 \
                and params["rsi_buy_low"] <= r.iloc[i] <= params["rsi_buy_high"]:
            entry = h + buf
            signals.append({"time": t, "direction": 1, "entry": entry,
                            "stop": entry - params["atr_stop_mult"] * bar_atr,
                            "target": entry + params["atr_target_mult"] * bar_atr,
                            "atr": bar_atr})
        elif c < recent_low.iloc[i] and trend.iloc[i] == -1 \
                and params["rsi_sell_low"] <= r.iloc[i] <= params["rsi_sell_high"]:
            entry = l - buf
            signals.append({"time": t, "direction": -1, "entry": entry,
                            "stop": entry + params["atr_stop_mult"] * bar_atr,
                            "target": entry - params["atr_target_mult"] * bar_atr,
                            "atr": bar_atr})
    return signals


def mean_reversion(df15, df1h, params: dict, fixed: dict) -> list[dict]:
    """Fades an RSI extreme back toward the mean instead of following the
    trend — buys oversold bounces, sells overbought fades. Deliberately
    counter to ema_pullback and breakout, which both require trend agreement."""
    slow = ema(df15["close"], fixed["ema_slow"])
    r = rsi(df15["close"], fixed["rsi_period"])
    a = atr_ind(df15, fixed["atr_period"])

    buf = params["entry_buffer_pips"] * PIP
    signals = []
    warmup = max(fixed["ema_slow"], fixed["atr_period"]) + 1
    for i in range(warmup, len(df15)):
        t = df15.index[i]
        if not session_ok(t, fixed):
            continue
        o, h, l, c = df15.iloc[i][["open", "high", "low", "close"]]
        bar_atr = a.iloc[i]
        if not candle_quality_ok(o, c, bar_atr, params):
            continue
        stretched_down = slow.iloc[i] - c > params["atr_stop_mult"] * bar_atr
        stretched_up = c - slow.iloc[i] > params["atr_stop_mult"] * bar_atr
        if stretched_down and r.iloc[i] < params["rsi_sell_low"] and c > o:
            entry = h + buf
            signals.append({"time": t, "direction": 1, "entry": entry,
                            "stop": entry - params["atr_stop_mult"] * bar_atr,
                            "target": entry + params["atr_target_mult"] * bar_atr,
                            "atr": bar_atr})
        elif stretched_up and r.iloc[i] > params["rsi_buy_high"] and c < o:
            entry = l - buf
            signals.append({"time": t, "direction": -1, "entry": entry,
                            "stop": entry + params["atr_stop_mult"] * bar_atr,
                            "target": entry - params["atr_target_mult"] * bar_atr,
                            "atr": bar_atr})
    return signals
