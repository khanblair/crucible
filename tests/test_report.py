"""Reporting tests: EAT timestamp conversion, the data-freshness field that
surfaces stale market data honestly, and the rolling-window paper-forward
money view that reports recent pace instead of an ever-growing all-time
total that can look frozen for days between fills.
"""
import datetime as dt
import json

from src.report import EAT, _data_freshness_field, _forward_money, now_eat

SETTINGS = {"reporting": {"lot_size": 0.1, "pip_value_usd_per_standard_lot": 10.0,
                          "daily_target_usd": 3.0, "rolling_window_days": 14}}


def _write_log(tmp_path, records):
    log_dir = tmp_path / "results" / "forward_log"
    log_dir.mkdir(parents=True)
    (log_dir / "signals.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")


def _closed(days_ago, pnl_pips):
    signal_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat()
    return {"kind": "resolution", "signal_time": signal_time,
            "outcome": "closed", "pnl_pips": pnl_pips}


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


def test_forward_money_none_when_no_log_file(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "ROOT", tmp_path)
    assert _forward_money(SETTINGS) is None


def test_forward_money_none_when_no_closed_trades(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _write_log(tmp_path, [
        {"kind": "signal", "time": "x", "direction": 1, "entry": 1.1,
         "stop": 1.0, "target": 1.2, "params_file": "x", "logged": "x"},
        {"kind": "resolution", "signal_time": "x", "outcome": "unfilled", "pnl_pips": 0.0},
    ])
    assert _forward_money(SETTINGS) is None


def test_forward_money_zero_when_only_stale_closes_outside_window(tmp_path, monkeypatch):
    """A real production case: the last actual fill happened weeks ago (data
    was frozen by the boundary-leak bug for days). This must be reported as
    honestly-zero-in-window, not fall back to stale all-time arithmetic."""
    import src.report as report
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _write_log(tmp_path, [_closed(30, -20.0)])
    m = _forward_money(SETTINGS)
    assert m == {"n": 0, "window_days": 14, "target_usd": 3.0}


def test_forward_money_windowed_excludes_old_trades(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _write_log(tmp_path, [_closed(30, -100.0), _closed(2, 10.0)])
    m = _forward_money(SETTINGS)
    assert m["n"] == 1
    assert m["net_usd"] > 0  # only the in-window +10 pip trade counts, not the old -100


def test_forward_money_span_reflects_days_since_oldest_in_window_trade(tmp_path, monkeypatch):
    import src.report as report
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _write_log(tmp_path, [_closed(5, 10.0)])
    m = _forward_money(SETTINGS)
    assert m["n"] == 1
    assert m["span_days"] == 5
