"""Exit-style modules: the genome-selectable choices for 'how a filled trade
is managed to close'. Every module receives the same inputs — the 15m frame,
the bar index where the trade filled, the signal (direction/entry/stop/target),
settings, and the intrabar-ordering table — and returns a list of legs, each
a (exit_price, fraction, is_stop_leg, bar_idx) tuple, in chronological order
of closure. Fractions need not sum to 1.0 — backtest.py marks any remaining
fraction out at the last close if the position never fully closes within the
available data. `bar_idx` (the bar at which that leg closed) lets backtest.py
scale swap by whatever fraction of the position was still open at each
rollover, rather than assuming the full position was open throughout.

Cost application (spread, slippage, swap) and metrics stay in backtest.py —
these modules decide trade *management* only, never cost.
"""
from src.strategy import resolve_first_touch


def atr_trail_half(df15, fill_idx: int, sig: dict, settings: dict,
                   intrabar: dict) -> list[tuple[float, float, bool, int]]:
    """Today's baseline exit: first target locks half the position and moves
    the stop to breakeven, then the remainder trails recent candle extremes."""
    fixed = settings["strategy_fixed"]
    d = sig["direction"]
    stop, target, be = sig["stop"], sig["target"], sig["entry"]

    legs: list[tuple[float, float, bool, int]] = []
    fraction_open, phase = 1.0, 1

    for j in range(fill_idx, len(df15)):
        bar = df15.iloc[j]
        t = df15.index[j]
        if phase == 1:
            stop_hit = bar["low"] <= stop if d == 1 else bar["high"] >= stop
            tp_hit = bar["high"] >= target if d == 1 else bar["low"] <= target
            if stop_hit and tp_hit:
                first = resolve_first_touch(t, d, intrabar)
            elif stop_hit:
                first = "stop"
            elif tp_hit:
                first = "target"
            else:
                continue
            if first == "stop":
                legs.append((stop, fraction_open, True, j))
                return legs
            legs.append((target, 0.5, False, j))   # TP1: lock half, stop to breakeven
            fraction_open, stop, phase = 0.5, be, 2
            be_hit = bar["low"] <= stop if d == 1 else bar["high"] >= stop
            if be_hit:                              # conservative: BE also hit in-bar
                legs.append((stop, fraction_open, True, j))
                return legs
        else:  # phase 2: trail the remainder on recent candle extremes
            lo = max(0, j - fixed["trail_lookback"])
            trail = (max(stop, df15["low"].iloc[lo:j].min()) if d == 1
                     else min(stop, df15["high"].iloc[lo:j].max()))
            stop = trail
            if (d == 1 and bar["low"] <= stop) or (d == -1 and bar["high"] >= stop):
                legs.append((stop, fraction_open, True, j))
                return legs

    return legs


def fixed_r_multiple(df15, fill_idx: int, sig: dict, settings: dict,
                     intrabar: dict) -> list[tuple[float, float, bool, int]]:
    """Simpler exit: one stop, one target, whichever is touched first (worst
    case if both fall in the same bar) closes the entire position. No partial
    lock, no trailing."""
    d = sig["direction"]
    stop, target = sig["stop"], sig["target"]

    for j in range(fill_idx, len(df15)):
        bar = df15.iloc[j]
        t = df15.index[j]
        stop_hit = bar["low"] <= stop if d == 1 else bar["high"] >= stop
        tp_hit = bar["high"] >= target if d == 1 else bar["low"] <= target
        if stop_hit and tp_hit:
            first = resolve_first_touch(t, d, intrabar)
        elif stop_hit:
            first = "stop"
        elif tp_hit:
            first = "target"
        else:
            continue
        price = stop if first == "stop" else target
        return [(price, 1.0, first == "stop", j)]

    return []
