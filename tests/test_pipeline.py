from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd

from gridcarbon import pipeline
from gridcarbon.load import upsert
from gridcarbon.timeutils import HALF_HOUR

NOW = datetime(2024, 6, 1, 12, 47, tzinfo=UTC)
INTENSITY = pipeline.DATASETS["intensity"]


def one_period(start: datetime) -> pd.DataFrame:
    return pd.DataFrame({
        "period_from": pd.to_datetime([start], utc=True),
        "period_to": pd.to_datetime([start + HALF_HOUR], utc=True),
        "forecast": [100], "actual": [100], "intensity_index": ["moderate"],
    })  # fmt: skip


def test_incremental_window_on_empty_table(con):
    start, end = pipeline.incremental_window(con, INTENSITY, NOW, lookback_days=3)
    assert start == datetime(2024, 5, 29, 12, 30, tzinfo=UTC)
    assert end == datetime(2024, 6, 1, 13, 0, tzinfo=UTC)  # includes the current half-hour


def test_incremental_window_anchors_on_latest_loaded_row(con):
    latest = datetime(2024, 4, 1, tzinfo=UTC)  # pipeline was down for two months
    upsert(con, INTENSITY.table, one_period(latest), INTENSITY.key_cols, "r1")
    start, _ = pipeline.incremental_window(con, INTENSITY, NOW, lookback_days=3)
    assert start == latest - timedelta(days=3)


def test_incremental_window_never_before_earliest(con):
    early = INTENSITY.earliest + timedelta(days=1)
    upsert(con, INTENSITY.table, one_period(early), INTENSITY.key_cols, "r1")
    start, _ = pipeline.incremental_window(con, INTENSITY, NOW, lookback_days=30)
    assert start == INTENSITY.earliest


def test_backfill_resumes_without_refetching(con):
    calls = []

    def fake_fetch(client, a, b):
        calls.append((a, b))
        return one_period(a)

    ds = replace(INTENSITY, fetch=fake_fetch, chunk_days=1)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    run = {"run_id": "r1", "rows_loaded": 0}
    pipeline.load_range(con, None, ds, start, start + timedelta(days=3), run, skip_loaded=True)
    assert len(calls) == 3 and run["rows_loaded"] == 3

    pipeline.load_range(con, None, ds, start, start + timedelta(days=4), run, skip_loaded=True)
    assert len(calls) == 4  # only the new day was fetched
    assert con.execute(f"SELECT count(*) FROM {ds.table}").fetchone()[0] == 4


def test_snapshot_forecasts_appends_by_issue_time(con, monkeypatch):
    issued = datetime(2024, 6, 1, 12, 30, tzinfo=UTC)
    official = pd.DataFrame({
        "issued_at": pd.to_datetime([issued] * 2, utc=True),
        "period_from": pd.to_datetime([issued, issued + HALF_HOUR], utc=True),
        "period_to": pd.to_datetime([issued + HALF_HOUR, issued + 2 * HALF_HOUR], utc=True),
        "forecast": [120, 110], "intensity_index": ["moderate", "moderate"],
    })  # fmt: skip
    monkeypatch.setattr(pipeline.ci, "fetch_official_forecast", lambda c, now: official)
    monkeypatch.setattr(
        pipeline.wx, "fetch_weather_forecast", lambda c, now: official.iloc[0:0][["issued_at"]]
    )
    run = {"run_id": "r1", "rows_loaded": 0}
    pipeline.snapshot_forecasts(con, None, NOW, run)
    pipeline.snapshot_forecasts(con, None, NOW, run)  # same issue time: replaced, not duplicated
    n = con.execute("SELECT count(*) FROM raw.official_forecast_snapshots").fetchone()[0]
    assert n == 2
