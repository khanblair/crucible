"""Data layer: fetch, clean, resample (Dukascopy + Twelve Data), feed
reconciliation. Offline mode (--build-intrabar) distills raw ticks into the
committed intrabar-ordering table — raw ticks NEVER enter the repository.

Decision data comes primarily from Dukascopy. Twelve Data is context/display
data always, AND — only when Dukascopy is confirmed unreachable for a genuine
trading day, never silently — a fallback decision-data source to bridge the
gap. Every fallback-sourced bar is logged and reported; nothing about it is
hidden. See CRUCIBLE.md's Data Pipeline section for the full policy.
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
_session.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/124.0.0.0 Safari/537.36")


def _fetch_day_minutes(day: dt.date, retries: int = 3) -> tuple[pd.DataFrame | None, dict]:
    """One day of Dukascopy 1-minute bid candles. Returns (frame_or_None, diag).

    diag['status'] is one of:
    - 'ok'          — real data, rows > 0
    - 'empty'       — HTTP 200 with no ticks: a genuine market holiday
    - 'rejected'    — non-200 response: the server pushed back. NOT the same
                      as a holiday — this is the signal Dukascopy is rate
                      limiting or blocking us, and must never be silently
                      treated as "no data today".
    - 'unreachable' — network failure after retries (timeout, DNS, etc.)
    """
    url = DUKA_URL.format(y=day.year, m=day.month - 1, d=day.day)  # month is 0-based
    last_exc = None
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=20)
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == retries - 1:
                return None, {"status": "unreachable", "detail": str(last_exc)}
            time.sleep(2 ** attempt)
    if r.status_code != 200:
        return pd.DataFrame(), {"status": "rejected", "http_status": r.status_code}
    if not r.content:
        return pd.DataFrame(), {"status": "empty"}
    raw = lzma.decompress(r.content)
    rec = struct.Struct(">5if")  # sec offset, open, close, low, high (points), volume
    rows = [rec.unpack_from(raw, o) for o in range(0, len(raw) - len(raw) % rec.size, rec.size)]
    base = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc)
    df = pd.DataFrame(rows, columns=["sec", "open", "close", "low", "high", "volume"])
    df.index = pd.to_datetime([base + dt.timedelta(seconds=int(s)) for s in df["sec"]])
    for c in ("open", "close", "low", "high"):
        df[c] = df[c] * POINT
    if df.empty:
        return pd.DataFrame(), {"status": "empty"}
    return df[["open", "high", "low", "close", "volume"]], {"status": "ok", "rows": len(df)}


def _resample(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df1m.resample(rule).agg(agg).dropna(subset=["open"])


def _weekdays_between(start_exclusive: dt.date, end_inclusive: dt.date) -> list[dt.date]:
    """Every FX-week weekday strictly after start, up to and including end."""
    days, d = [], start_exclusive + dt.timedelta(days=1)
    while d <= end_inclusive:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def load_candles() -> tuple[pd.DataFrame, pd.DataFrame]:
    f15, f1h = CANDLES / "eurusd_15m.csv", CANDLES / "eurusd_1h.csv"
    if not f15.exists() or not f1h.exists():
        raise FileNotFoundError("no candle data — run `python -m src.data --refresh` first")
    read = lambda p: pd.read_csv(p, index_col=0, parse_dates=True)
    return read(f15), read(f1h)


def check_staleness(today: dt.date | None = None) -> dict:
    """How far behind is the committed candle data? A pure read of the
    checked-out file — no network call — safe to use from reporting code
    without triggering a fetch. Reused by refresh() itself and by report.py."""
    today = today or dt.date.today()
    f15 = CANDLES / "eurusd_15m.csv"
    if not f15.exists():
        return {"last_date": None, "stale_trading_days": None}
    last_date = pd.read_csv(f15, index_col=0, parse_dates=True).index[-1].date()
    expected_through = today - dt.timedelta(days=1)  # Dukascopy's usual ~1-day publish delay
    missing = _weekdays_between(last_date, expected_through)
    return {"last_date": last_date.isoformat(), "stale_trading_days": len(missing)}


def _merge_and_write(fresh: pd.DataFrame, f15: pathlib.Path) -> None:
    """Resample fresh 1-minute bars to 15m/1h, merge with what's committed,
    dedup keeping the newest, and write at stable rounded precision so a
    rewrite of unchanged history is byte-identical (design rule: daily
    commits should be appends, not 88k-line churn)."""
    for rule, path in (("15min", f15), ("1h", CANDLES / "eurusd_1h.csv")):
        new = _resample(fresh, rule)
        if path.exists():
            old = pd.read_csv(path, index_col=0, parse_dates=True)
            new = pd.concat([old, new])
            new = new[~new.index.duplicated(keep="last")].sort_index()
        new[["open", "high", "low", "close"]] = new[["open", "high", "low", "close"]].round(5)
        new["volume"] = new["volume"].round(2)
        new.to_csv(path)


def fetch_twelve_data_range(interval: str, start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    """Fallback decision-data source, used ONLY to bridge a confirmed
    Dukascopy gap — real market data, but a different vendor's aggregation
    methodology (see the monthly reconciliation job), so it is never used
    silently. Returns an empty frame on any failure; callers must not treat
    that as fatal, only as "the fallback didn't help this time"."""
    key = os.environ.get("TWELVE_DATA_KEY", "")
    if not key:
        return pd.DataFrame()
    try:
        r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
            "symbol": "EUR/USD", "interval": interval, "timezone": "UTC",
            "start_date": start_date.isoformat(),
            "end_date": (end_date + dt.timedelta(days=1)).isoformat(),
            "outputsize": 5000, "apikey": key})
        payload = r.json()
    except (requests.RequestException, ValueError):
        return pd.DataFrame()
    values = payload.get("values", [])
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    # tz-aware UTC to match the committed candle CSVs exactly (they're written
    # with a +00:00 offset) — mixing naive and aware indices is what crashed
    # reconcile() in production; fixed there too, see below.
    df.index = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    # Twelve Data often omits volume entirely for forex pairs — df.get() with a
    # scalar default returns a bare int (no .fillna()), not a Series; only
    # coerce when the column genuinely exists, else it's legitimately zero.
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    else:
        df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def refresh(years_back: int = 3) -> dict:
    """Extend the stored 15m/1h candle files up to yesterday (Dukascopy
    publishes with roughly a one-day delay). Returns a summary dict — always,
    even on a fully quiet run — so callers (and reporting) can tell genuine
    advancement from a stall, rather than trusting a print statement that
    used to claim success whether or not anything actually changed."""
    f15 = CANDLES / "eurusd_15m.csv"
    if f15.exists():
        previous_last_date = pd.read_csv(f15, index_col=0, parse_dates=True).index[-1].date()
    else:
        previous_last_date = dt.date.today() - dt.timedelta(days=365 * years_back)
    end = dt.date.today() - dt.timedelta(days=1)

    # Weekdays from previous_last_date (inclusive — re-try it in case it was
    # only partially published) through end (inclusive). Works identically
    # for a bootstrap (no file yet): previous_last_date is already set to the
    # desired history start above.
    frames, rejected, unreachable, done = [], [], [], 0
    days_to_try = _weekdays_between(previous_last_date - dt.timedelta(days=1), end)
    total = len(days_to_try)
    for day in days_to_try:
        df, diag = _fetch_day_minutes(day)
        done += 1
        if diag["status"] == "ok":
            frames.append(df)
        elif diag["status"] == "rejected":
            rejected.append({"date": day.isoformat(), "http_status": diag["http_status"]})
        elif diag["status"] == "unreachable":
            unreachable.append(day.isoformat())
        # 'empty' (genuine holiday) needs no bookkeeping — it's the expected case
        if done % 100 == 0:
            print(f"  {done}/{total} days fetched ({len(rejected)} rejected, "
                 f"{len(unreachable)} unreachable)", flush=True)
        time.sleep(0.15)  # be a polite client on the free feed, not a scraper hammering it

    if rejected:
        statuses = ", ".join(f"{r['date']} (HTTP {r['http_status']})" for r in rejected[:10])
        print(f"WARNING: Dukascopy REJECTED {len(rejected)} request(s), not a holiday: {statuses}"
             f"{'…' if len(rejected) > 10 else ''} — the feed is pushing back, not just quiet")
    if unreachable:
        print(f"WARNING: {len(unreachable)} day(s) unreachable after retries: "
             f"{', '.join(unreachable[:10])}{'…' if len(unreachable) > 10 else ''}")
    if len(rejected) + len(unreachable) > max(total // 3, 3):
        raise RuntimeError("too many failed days — Dukascopy feed unhealthy, aborting refresh")

    if frames:
        _merge_and_write(pd.concat(frames), f15)

    status = check_staleness()
    new_last_date = dt.date.fromisoformat(status["last_date"]) if status["last_date"] else previous_last_date
    advanced = new_last_date > previous_last_date
    fallback_used, fallback_dates = False, []

    still_missing = _weekdays_between(new_last_date, end)
    if still_missing and (rejected or unreachable):
        # Dukascopy is confirmed struggling (not just a quiet holiday stretch) —
        # bridge the specific missing weekdays with Twelve Data, clearly flagged.
        gap_start, gap_end = still_missing[0], still_missing[-1]
        fb15 = fetch_twelve_data_range("15min", gap_start, gap_end)
        if not fb15.empty:
            _merge_and_write(fb15, f15)
            fallback_used = True
            fallback_dates = [d.isoformat() for d in still_missing]
            print(f"FALLBACK: Dukascopy unavailable for {gap_start}..{gap_end}; "
                 f"used Twelve Data to bridge the gap (real market data, different vendor)")
            status = check_staleness()
            new_last_date = dt.date.fromisoformat(status["last_date"])
            advanced = new_last_date > previous_last_date

    summary = {"date": dt.date.today().isoformat(), "type": "refresh",
              "previous_last_date": previous_last_date.isoformat(),
              "new_last_date": new_last_date.isoformat(), "advanced": advanced,
              "stale_trading_days": status["stale_trading_days"],
              "rejected_days": rejected, "unreachable_days": unreachable,
              "fallback_used": fallback_used, "fallback_dates": fallback_dates}
    out = ROOT / "results" / "runs" / f"refresh_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")

    if advanced:
        print(f"candles genuinely advanced: {previous_last_date} -> {new_last_date}")
    else:
        print(f"no genuine advancement — still at {new_last_date} "
             f"({status['stale_trading_days']} trading day(s) stale)")
    return summary


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
    # tz-aware UTC to match df1h's index (read from a +00:00-suffixed CSV) —
    # a bare pd.Timestamp(str) here is tz-naive and crashes pd.concat with
    # "Cannot join tz-naive with tz-aware DatetimeIndex". Real production bug.
    td = pd.Series({pd.Timestamp(v["datetime"], tz="UTC"): float(v["close"])
                    for v in values}).sort_index()
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
        print(json.dumps(refresh(), indent=2))
    if args.context:
        fetch_context()
    if args.reconcile:
        print(json.dumps(reconcile(), indent=2))
    if args.build_intrabar:
        build_intrabar(args.build_intrabar)
