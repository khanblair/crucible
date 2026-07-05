"""Regression test for a real production bug: refresh() crashed with
'No objects to concatenate' whenever every fetched day came back empty from
Dukascopy (a holiday, or today's bar not yet published) — which only shows
up on a narrow refresh window, exactly what a same-day re-run hits.
"""
import datetime as dt

import pandas as pd

import src.data as data


def test_refresh_handles_every_fetched_day_being_empty(tmp_path, monkeypatch):
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    monkeypatch.setattr(data, "CANDLES", candles_dir)

    # seed an existing 15m file whose last bar is yesterday, so refresh()'s
    # window is exactly one day wide — the narrow-window case that broke
    seed_idx = pd.date_range(dt.date.today() - dt.timedelta(days=2), periods=4, freq="15min")
    seed = pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},
                        index=seed_idx)
    seed.to_csv(candles_dir / "eurusd_15m.csv")

    # every fetched day comes back empty (e.g. a holiday, or not yet published)
    monkeypatch.setattr(data, "_fetch_day_minutes", lambda day, retries=3: pd.DataFrame())

    data.refresh()   # must not raise ValueError("No objects to concatenate")

    # no new data existed, so the seeded file is untouched
    result = pd.read_csv(candles_dir / "eurusd_15m.csv", index_col=0, parse_dates=True)
    assert len(result) == 4


def test_refresh_still_writes_data_when_some_days_are_empty(tmp_path, monkeypatch):
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    monkeypatch.setattr(data, "CANDLES", candles_dir)

    start = dt.date.today() - dt.timedelta(days=6)
    seed_idx = pd.date_range(start, periods=4, freq="15min")
    seed = pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 1.0},
                        index=seed_idx)
    seed.to_csv(candles_dir / "eurusd_15m.csv")

    real_bars = pd.date_range(start + dt.timedelta(days=1), periods=96, freq="15min")
    real_frame = pd.DataFrame({"open": 1.2, "high": 1.21, "low": 1.19, "close": 1.2,
                               "volume": 5.0}, index=real_bars)

    def fake_fetch(day, retries=3):
        return real_frame if day == start + dt.timedelta(days=1) else pd.DataFrame()

    monkeypatch.setattr(data, "_fetch_day_minutes", fake_fetch)
    data.refresh()

    result = pd.read_csv(candles_dir / "eurusd_15m.csv", index_col=0, parse_dates=True)
    assert len(result) > 4   # the real day's bars were appended, not lost
