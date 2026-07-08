"""Reporting tests: EAT timestamp conversion and the data-freshness field
that surfaces stale market data honestly in Discord/dashboard instead of
silently re-presenting the same analysis as if it were fresh.
"""
import datetime as dt
import json

from src.report import EAT, _data_freshness_field, now_eat


def test_eat_is_utc_plus_3_fixed_offset():
    assert EAT.utcoffset(None) == dt.timedelta(hours=3)


def test_now_eat_matches_utc_plus_3():
    eat = now_eat()
    utc = dt.datetime.now(dt.timezone.utc)
    # allow a couple seconds of drift between the two now() calls
    assert abs((eat.astimezone(dt.timezone.utc) - utc).total_seconds()) < 5
    assert eat.utcoffset() == dt.timedelta(hours=3)


def test_data_freshness_field_none_when_no_refresh_log(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "RUNS", tmp_path)
    assert _data_freshness_field() is None


def test_data_freshness_field_none_when_current(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "RUNS", tmp_path)
    (tmp_path / "refresh_2026-07-08.json").write_text(json.dumps(
        {"stale_trading_days": 0, "new_last_date": "2026-07-07"}))
    assert _data_freshness_field() is None


def test_data_freshness_field_present_when_stale(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "RUNS", tmp_path)
    (tmp_path / "refresh_2026-07-08.json").write_text(json.dumps(
        {"stale_trading_days": 2, "new_last_date": "2026-07-03", "fallback_used": False}))
    field = _data_freshness_field()
    assert field is not None
    assert "2 trading day" in field["value"]
    assert "2026-07-03" in field["value"]


def test_data_freshness_field_mentions_fallback(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "RUNS", tmp_path)
    (tmp_path / "refresh_2026-07-08.json").write_text(json.dumps(
        {"stale_trading_days": 1, "new_last_date": "2026-07-06", "fallback_used": True}))
    field = _data_freshness_field()
    assert "fallback" in field["value"].lower()
