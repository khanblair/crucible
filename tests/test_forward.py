"""Paper-forward signal resolution: a signal near the edge of currently
available data must not be judged "unfilled" until it has actually had its
full entry_valid_bars window to fill — otherwise every run permanently
mis-resolves its most recent signals purely because more bars don't exist
yet, which quietly undercounts fills forever (resolved signals are never
re-checked).
"""
import json

import pandas as pd
import pytest

import src.forward as forward

SETTINGS = {"strategy_fixed": {"entry_valid_bars": 4}}


def _df15(n):
    idx = pd.date_range("2026-07-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1}, index=idx)


@pytest.fixture
def isolated_forward(tmp_path, monkeypatch):
    log_dir = tmp_path / "results" / "forward_log"
    log_dir.mkdir(parents=True)
    active_dir = tmp_path / "config" / "regimes"
    active_dir.mkdir(parents=True)
    (active_dir / "trending.json").write_text(json.dumps({"params": {}, "genome": "baseline"}))

    monkeypatch.setattr(forward, "ROOT", tmp_path)
    monkeypatch.setattr(forward, "LOG", log_dir / "signals.jsonl")
    monkeypatch.setattr(forward, "validate_active",
                         lambda: {"params_file": "config/regimes/trending.json"})
    monkeypatch.setattr(forward, "load_genome",
                         lambda name: {"exit_style": "atr_trail_half"})
    monkeypatch.setattr(forward, "assemble", lambda genome: (lambda *a, **k: [], None))
    monkeypatch.setattr(forward, "load_intrabar", lambda: {})
    return log_dir / "signals.jsonl"


def _seed_open_signal(log_path, sig_time):
    log_path.write_text(json.dumps({
        "kind": "signal", "time": sig_time, "direction": 1, "entry": 1.1,
        "stop": 1.05, "target": 1.15, "params_file": "config/regimes/trending.json",
        "logged": "2026-07-01"}) + "\n")


def test_edge_signal_stays_open_when_fill_window_not_yet_elapsed(isolated_forward, monkeypatch):
    df15 = _df15(10)
    sig_time = str(df15.index[8])  # needs bars 9..12; only bar 9 exists (1 of 4)
    _seed_open_signal(isolated_forward, sig_time)
    monkeypatch.setattr(forward, "simulate_trade", lambda *a, **k: None)

    forward.log_signals(df15, df15, SETTINGS)

    records = [json.loads(l) for l in isolated_forward.read_text().splitlines() if l.strip()]
    assert [r for r in records if r["kind"] == "resolution"] == []


def test_signal_marked_unfilled_once_full_window_has_elapsed(isolated_forward, monkeypatch):
    df15 = _df15(20)
    sig_time = str(df15.index[5])  # needs bars 6..9; all exist within 20 bars
    _seed_open_signal(isolated_forward, sig_time)
    monkeypatch.setattr(forward, "simulate_trade", lambda *a, **k: None)

    forward.log_signals(df15, df15, SETTINGS)

    records = [json.loads(l) for l in isolated_forward.read_text().splitlines() if l.strip()]
    resolutions = [r for r in records if r["kind"] == "resolution"]
    assert len(resolutions) == 1
    assert resolutions[0]["outcome"] == "unfilled"
