"""Data layer: fetch, clean, resample (Dukascopy + Twelve Data), feed
reconciliation. Offline mode (--build-intrabar) distills raw ticks into the
committed intrabar-ordering table — raw ticks NEVER enter the repository.

Decision data comes exclusively from Dukascopy. Twelve Data is context/display
only and never feeds an accept/reject decision.
"""
import datetime as dt
import io
import json
import lzma
import os
import pathlib
import struct
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANDLES = ROOT / "data" / "candles"
POINT = 1e-5  # Dukascopy prices are integer points
DUKA_URL = "https://datafeed.dukascopy.com/datafeed/EURUSD/{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"


_session = requests.Session()
_session.headers["User-Agent"] = "Mozilla/5.0 (crucible data layer)"


def _fetch_day_minutes(day: dt.date, retries: int = 3) -> pd.DataFrame | None:
    """One day of Dukascopy 1-minute bid candles. Empty frame on holidays,
    None when the feed stays unreachable after retries — the caller skips the
    day and reports it rather than losing the whole refresh to one bad request."""
    url = DUKA_URL.format(y=day.year, m=day.month - 1, d=day.day)  # month is 0-based
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=20)
            break
        except requests.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt)
    if r.status_code != 200 or not r.content:
        return pd.DataFrame()
    raw = lzma.decompress(r.content)
    rec = struct.Struct(">5if")  # sec offset, open, close, low, high (points), volume
    rows = [rec.unpack_from(raw, o) for o in range(0, len(raw) - len(raw) % rec.size, rec.size)]
    base = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc)
    df = pd.DataFrame(rows, columns=["sec", "open", "close", "low", "high", "volume"])
    df.index = pd.to_datetime([base + dt.timedelta(seconds=int(s)) for s in df["sec"]])
    for c in ("open", "close", "low", "high"):
        df[c] = df[c] * POINT
    return df[["open", "high", "low", "close", "volume"]]


def _resample(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df1m.resample(rule).agg(agg).dropna(subset=["open"])


def load_candles() -> tuple[pd.DataFrame, pd.DataFrame]:
    f15, f1h = CANDLES / "eurusd_15m.csv", CANDLES / "eurusd_1h.csv"
    if not f15.exists() or not f1h.exists():
        raise FileNotFoundError("no candle data — run `python -m src.data --refresh` first")
    read = lambda p: pd.read_csv(p, index_col=0, parse_dates=True)
    return read(f15), read(f1h)


def refresh(years_back: int = 3) -> None:
    """Extend the stored 15m/1h candle files up to yesterday (Dukascopy
    publishes with roughly a one-day delay)."""
    f15 = CANDLES / "eurusd_15m.csv"
    if f15.exists():
        start = pd.read_csv(f15, index_col=0, parse_dates=True).index[-1].date()
    else:
        start = dt.date.today() - dt.timedelta(days=365 * years_back)
    end = dt.date.today() - dt.timedelta(days=1)
    frames, failed, day, done = [], [], start, 0
    total = ((end - start).days * 5) // 7 + 1
    while day <= end:
        if day.weekday() < 5:  # FX week; sparse Sunday bars come with Monday files
            df = _fetch_day_minutes(day)
            if df is None:
                failed.append(day.isoformat())
            else:
                frames.append(df)
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{total} days fetched ({len(failed)} failed)", flush=True)
        day += dt.timedelta(days=1)
    if failed:
        print(f"WARNING: {len(failed)} day(s) unreachable after retries: "
              f"{', '.join(failed[:10])}{'…' if len(failed) > 10 else ''}")
    if len(failed) > max(total // 10, 3):
        raise RuntimeError("too many failed days — feed unhealthy, aborting refresh")
    fresh = pd.concat([f for f in frames if not f.empty]) if frames else pd.DataFrame()
    if fresh.empty:
        print("no new Dukascopy data")
        return
    for rule, path in (("15min", f15), ("1h", CANDLES / "eurusd_1h.csv")):
        new = _resample(fresh, rule)
        if path.exists():
            old = pd.read_csv(path, index_col=0, parse_dates=True)
            new = pd.concat([old, new])
            new = new[~new.index.duplicated(keep="last")].sort_index()
        # canonical precision so a rewrite of unchanged history is byte-identical:
        # 5 decimals is full EUR/USD point resolution, and stable floats keep the
        # daily commit to appended rows instead of an 88k-line churn
        new[["open", "high", "low", "close"]] = new[["open", "high", "low", "close"]].round(5)
        new["volume"] = new["volume"].round(2)
        new.to_csv(path)
    print(f"candles refreshed through {end.isoformat()}")


def fetch_context() -> dict:
    """Twelve Data recent candles for the dashboard/Discord status. Display only."""
    key = os.environ.get("TWELVE_DATA_KEY", "")
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
        "symbol": "EUR/USD", "interval": "15min", "outputsize": 96, "apikey": key})
    payload = r.json()
    out = ROOT / "results" / "runs" / f"context_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps({"date": dt.date.today().isoformat(),
                               "source": "twelvedata", "data": payload}, indent=2) + "\n")
    return payload


def reconcile() -> dict:
    """Monthly cross-feed drift check over the overlap window (in pips)."""
    settings = json.loads((ROOT / "config" / "settings.json").read_text())
    _, df1h = load_candles()
    key = os.environ.get("TWELVE_DATA_KEY", "")
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
        "symbol": "EUR/USD", "interval": "1h", "outputsize": 500, "apikey": key})
    values = r.json().get("values", [])
    td = pd.Series({pd.Timestamp(v["datetime"]): float(v["close"]) for v in values}).sort_index()
    joined = pd.concat([df1h["close"], td], axis=1, keys=["duka", "td"]).dropna()
    diff = float((joined["duka"] - joined["td"]).abs().mean() / 1e-4) if len(joined) else 0.0
    alert = diff > settings["reconciliation"]["max_mean_abs_diff_pips"]
    log = {"date": dt.date.today().isoformat(), "type": "reconciliation",
           "overlap_bars": int(len(joined)), "mean_abs_diff_pips": round(diff, 3), "alert": alert}
    out = ROOT / "results" / "runs" / f"reconcile_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps(log, indent=2) + "\n")
    return log


def build_intrabar(ticks_dir: str) -> None:
    """Offline, one-time pass: scan local raw Dukascopy tick files (.bi5) and
    record, per 15m bar, which extreme was touched first. Output is the
    kilobyte-scale committed table; the gigabyte ticks stay outside the repo."""
    rec = struct.Struct(">3i2f")  # ms offset, ask points, bid points, ask vol, bid vol
    rows = []
    for path in sorted(pathlib.Path(ticks_dir).rglob("*h_ticks.bi5")):
        y, m, d, h = path.parts[-4], path.parts[-3], path.parts[-2], int(path.stem[:2])
        base = dt.datetime(int(y), int(m) + 1, int(d), h, tzinfo=dt.timezone.utc)
        raw = lzma.decompress(path.read_bytes())
        ticks = pd.DataFrame(
            [rec.unpack_from(raw, o) for o in range(0, len(raw) - len(raw) % rec.size, rec.size)],
            columns=["ms", "ask", "bid", "av", "bv"])
        if ticks.empty:
            continue
        ticks.index = pd.to_datetime([base + dt.timedelta(milliseconds=int(v)) for v in ticks["ms"]])
        for bar_time, grp in ticks.groupby(pd.Grouper(freq="15min")):
            if grp.empty:
                continue
            first = "high_first" if grp["bid"].idxmax() <= grp["bid"].idxmin() else "low_first"
            rows.append({"time": bar_time.tz_localize(None), "first": first})
    out = ROOT / "data" / "intrabar" / "ordering.parquet"
    pd.DataFrame(rows).to_parquet(out, index=False)
    print(f"intrabar table written: {len(rows)} bars -> {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Crucible data layer")
    ap.add_argument("--refresh", action="store_true", help="extend Dukascopy candles")
    ap.add_argument("--context", action="store_true", help="fetch Twelve Data context candles")
    ap.add_argument("--reconcile", action="store_true", help="monthly cross-feed drift check")
    ap.add_argument("--build-intrabar", metavar="TICKS_DIR",
                    help="offline: build data/intrabar/ordering.parquet from local raw ticks")
    args = ap.parse_args()
    if args.refresh:
        refresh()
    if args.context:
        fetch_context()
    if args.reconcile:
        print(json.dumps(reconcile(), indent=2))
    if args.build_intrabar:
        build_intrabar(args.build_intrabar)
