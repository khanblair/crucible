"""Data layer tests: candle refresh robustness, staleness detection, the
Twelve Data fallback path, and the tz-mismatch fix in reconcile(). Several of
these are regression tests for real production bugs — a same-day re-run with
nothing new to fetch, a Dukascopy outage silently masquerading as a holiday,
and a tz-naive/tz-aware concat crash — each reproduced against the pre-fix
code before being fixed, per this project's own verification discipline.
"""
import datetime as dt

import pandas as pd
import pytest

import src.data as data


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """refresh() writes its summary JSON to ROOT/results/runs/, not just
    candles to CANDLES — patching only CANDLES (as an earlier version of
    these tests did) leaves that write landing in the real repository.
    This fixture isolates both, matching how refresh() actually behaves."""
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    (tmp_path / "results" / "runs").mkdir(parents=True)
    monkeypatch.setattr(data, "CANDLES", candles_dir)
    monkeypatch.setattr(data, "ROOT", tmp_path)
    return candles_dir


def _seed_csv(candles_dir, last_date: dt.date, n_bars: int = 4) -> None:
    """A minimal, tz-aware (UTC, matching real committed CSVs) seed file."""
    idx = pd.date_range(dt.datetime.combine(last_date, dt.time(23, 0), tzinfo=dt.timezone.utc),
                        periods=n_bars, freq="15min")
    seed = pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},
                        index=idx)
    seed.to_csv(candles_dir / "eurusd_15m.csv")


def _diag_fetch(status_by_day: dict, ok_frame_factory=None):
    """Build a fake _fetch_day_minutes(day) -> (frame_or_None, diag) matching
    the real function's (post-fix) tuple signature."""
    def fake(day, retries=3):
        status = status_by_day.get(day, "empty")
        if status == "unreachable":
            return None, {"status": "unreachable", "detail": "timeout"}
        if status == "rejected":
            return pd.DataFrame(), {"status": "rejected", "http_status": 503}
        if status == "ok":
            frame = ok_frame_factory(day) if ok_frame_factory else pd.DataFrame(
                {"open": 1.2, "high": 1.21, "low": 1.19, "close": 1.2, "volume": 5.0},
                index=pd.date_range(dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc),
                                    periods=96, freq="15min"))
            return frame, {"status": "ok", "rows": len(frame)}
        return pd.DataFrame(), {"status": "empty"}
    return fake


# ------------------------------------------------------------ _weekdays_between
def test_weekdays_between_skips_weekend():
    # Fri 2026-07-03 (exclusive) through Mon 2026-07-06 (inclusive): only Monday
    days = data._weekdays_between(dt.date(2026, 7, 3), dt.date(2026, 7, 6))
    assert days == [dt.date(2026, 7, 6)]


def test_weekdays_between_empty_when_end_before_start():
    assert data._weekdays_between(dt.date(2026, 7, 6), dt.date(2026, 7, 3)) == []


def test_weekdays_between_all_five_weekdays():
    days = data._weekdays_between(dt.date(2026, 6, 28), dt.date(2026, 7, 3))  # Sun -> Fri
    assert days == [dt.date(2026, 6, 29), dt.date(2026, 6, 30), dt.date(2026, 7, 1),
                    dt.date(2026, 7, 2), dt.date(2026, 7, 3)]


# -------------------------------------------------------------- check_staleness
def test_check_staleness_zero_when_current(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CANDLES", tmp_path)
    _seed_csv(tmp_path, dt.date(2026, 7, 6))
    status = data.check_staleness(today=dt.date(2026, 7, 7))   # Tue; expected through Mon 07-06
    assert status["stale_trading_days"] == 0


def test_check_staleness_counts_missing_trading_days(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CANDLES", tmp_path)
    _seed_csv(tmp_path, dt.date(2026, 7, 3))   # Friday
    status = data.check_staleness(today=dt.date(2026, 7, 8))   # Wed; expected through Tue 07-07
    assert status["stale_trading_days"] == 2   # Mon 07-06 and Tue 07-07 both missing


def test_check_staleness_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CANDLES", tmp_path)
    assert data.check_staleness()["stale_trading_days"] is None


# -------------------------------------------------------------------- refresh()
def test_refresh_handles_every_fetched_day_being_empty(isolated_data, monkeypatch):
    """Regression: 'No objects to concatenate' when every fetched day came
    back empty — only shows up on a narrow window, exactly a same-day re-run."""
    _seed_csv(isolated_data, dt.date.today() - dt.timedelta(days=2))
    monkeypatch.setattr(data, "_fetch_day_minutes", _diag_fetch({}))   # everything 'empty'

    summary = data.refresh()   # must not raise ValueError("No objects to concatenate")

    assert summary["advanced"] is False
    result = pd.read_csv(isolated_data / "eurusd_15m.csv", index_col=0, parse_dates=True)
    assert len(result) == 4   # untouched


def test_refresh_advances_when_a_new_day_succeeds(isolated_data, monkeypatch):
    start = dt.date.today() - dt.timedelta(days=6)
    while start.weekday() >= 5:
        start -= dt.timedelta(days=1)
    _seed_csv(isolated_data, start)
    new_day = data._weekdays_between(start, dt.date.today() - dt.timedelta(days=1))[-1]

    monkeypatch.setattr(data, "_fetch_day_minutes", _diag_fetch({new_day: "ok"}))
    summary = data.refresh()

    assert summary["advanced"] is True
    assert summary["new_last_date"] == new_day.isoformat()
    result = pd.read_csv(isolated_data / "eurusd_15m.csv", index_col=0, parse_dates=True)
    assert len(result) > 4


def test_refresh_reports_rejected_separately_from_unreachable(isolated_data, monkeypatch):
    """A non-200 response must never be silently folded into 'holiday' —
    that's the exact ambiguity that hid a real Dukascopy outage in production."""
    start = dt.date.today() - dt.timedelta(days=3)
    while start.weekday() >= 5:
        start -= dt.timedelta(days=1)
    _seed_csv(isolated_data, start)
    days = data._weekdays_between(start, dt.date.today() - dt.timedelta(days=1))

    status_by_day = {}
    if days:
        status_by_day[days[0]] = "rejected"
    monkeypatch.setattr(data, "_fetch_day_minutes", _diag_fetch(status_by_day))
    monkeypatch.setattr(data, "fetch_twelve_data_range", lambda *a, **k: pd.DataFrame())

    summary = data.refresh()
    assert any(r["date"] == days[0].isoformat() for r in summary["rejected_days"])
    assert summary["unreachable_days"] == []


def test_refresh_falls_back_to_twelve_data_when_dukascopy_confirmed_down(isolated_data, monkeypatch):
    start = dt.date.today() - dt.timedelta(days=4)
    while start.weekday() >= 5:
        start -= dt.timedelta(days=1)
    _seed_csv(isolated_data, start)
    days = data._weekdays_between(start, dt.date.today() - dt.timedelta(days=1))
    assert days, "test needs at least one weekday gap to be meaningful"

    # every real day rejected (503) -> confirmed outage, not a holiday
    monkeypatch.setattr(data, "_fetch_day_minutes",
                        _diag_fetch({d: "rejected" for d in days}))

    def fake_fallback(interval, start_date, end_date):
        idx = pd.date_range(dt.datetime.combine(start_date, dt.time(), tzinfo=dt.timezone.utc),
                            periods=96, freq="15min")
        return pd.DataFrame({"open": 1.3, "high": 1.31, "low": 1.29, "close": 1.3,
                             "volume": 0.0}, index=idx)
    monkeypatch.setattr(data, "fetch_twelve_data_range", fake_fallback)

    summary = data.refresh()
    assert summary["fallback_used"] is True
    assert summary["advanced"] is True
    result = pd.read_csv(isolated_data / "eurusd_15m.csv", index_col=0, parse_dates=True)
    assert len(result) > 4


def test_refresh_does_not_fall_back_on_a_genuine_quiet_stretch(isolated_data, monkeypatch):
    """No rejected/unreachable days at all (e.g. a real holiday) must NOT
    trigger the fallback — that would defeat the whole point of Dukascopy
    being the primary, trusted source."""
    _seed_csv(isolated_data, dt.date.today() - dt.timedelta(days=2))
    monkeypatch.setattr(data, "_fetch_day_minutes", _diag_fetch({}))   # all genuinely empty
    called = []
    monkeypatch.setattr(data, "fetch_twelve_data_range",
                        lambda *a, **k: called.append(1) or pd.DataFrame())

    data.refresh()
    assert called == []


def test_refresh_raises_when_too_many_days_fail(isolated_data, monkeypatch):
    _seed_csv(isolated_data, dt.date.today() - dt.timedelta(days=20))
    days = data._weekdays_between(dt.date.today() - dt.timedelta(days=21),
                                  dt.date.today() - dt.timedelta(days=1))
    monkeypatch.setattr(data, "_fetch_day_minutes",
                        _diag_fetch({d: "unreachable" for d in days}))
    with pytest.raises(RuntimeError):
        data.refresh()


# ------------------------------------------------------- fetch_twelve_data_range
def test_fetch_twelve_data_range_returns_tz_aware_utc_index(monkeypatch):
    class FakeResponse:
        def json(self):
            return {"values": [{"datetime": "2026-07-06 10:00:00", "open": "1.1",
                                "high": "1.11", "low": "1.09", "close": "1.10"}]}
    monkeypatch.setattr(data.os.environ, "get", lambda k, d="": "fake-key")
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: FakeResponse())

    df = data.fetch_twelve_data_range("15min", dt.date(2026, 7, 6), dt.date(2026, 7, 6))
    assert df.index.tz is not None
    assert str(df.index.tz) == "UTC"


def test_fetch_twelve_data_range_empty_without_api_key(monkeypatch):
    monkeypatch.setattr(data.os.environ, "get", lambda k, d="": "")
    assert data.fetch_twelve_data_range("15min", dt.date(2026, 7, 6), dt.date(2026, 7, 6)).empty


# ---------------------------------------------------------------------- reconcile
def test_reconcile_does_not_crash_on_tz_naive_twelve_data_timestamps(tmp_path, monkeypatch):
    """Regression: reconcile() crashed in production with 'Cannot join
    tz-naive with tz-aware DatetimeIndex' — Twelve Data's bare datetime
    strings parsed tz-naive, while the committed (Dukascopy) candles are
    tz-aware UTC."""
    monkeypatch.setattr(data, "CANDLES", tmp_path)
    monkeypatch.setattr(data, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text(
        '{"reconciliation": {"max_mean_abs_diff_pips": 0.5}}')
    (tmp_path / "results" / "runs").mkdir(parents=True)
    idx = pd.date_range("2026-07-06", periods=5, freq="1h", tz="UTC")
    pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},
                index=idx).to_csv(tmp_path / "eurusd_15m.csv")
    pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},
                index=idx).to_csv(tmp_path / "eurusd_1h.csv")

    class FakeResponse:
        def json(self):
            return {"values": [{"datetime": str(idx[0])[:19], "close": "1.1"}]}
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: FakeResponse())

    log = data.reconcile()   # must not raise TypeError
    assert "mean_abs_diff_pips" in log
