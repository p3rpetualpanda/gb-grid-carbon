from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

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
    wind = pd.DataFrame({
        "issued_at": pd.to_datetime([issued], utc=True),
        "period_from": pd.to_datetime([issued], utc=True),
        "forecast_mw": [9000.0], "capacity_mw": [25502.0],
    })  # fmt: skip
    monkeypatch.setattr(pipeline.neso, "fetch_wind_14d_forecast", lambda c: wind)
    run = {"run_id": "r1", "rows_loaded": 0}
    pipeline.snapshot_forecasts(con, None, NOW, run)
    pipeline.snapshot_forecasts(con, None, NOW, run)  # same issue time: replaced, not duplicated
    n = con.execute("SELECT count(*) FROM raw.official_forecast_snapshots").fetchone()[0]
    assert n == 2
    assert con.execute("SELECT count(*) FROM raw.neso_wind_14d_snapshots").fetchone()[0] == 1


def test_backfill_retries_a_window_that_came_back_empty(con):
    calls = []

    def fake_fetch(client, a, b):
        calls.append((a, b))
        return one_period(a) if len(calls) > 1 else one_period(a).iloc[0:0]

    ds = replace(INTENSITY, fetch=fake_fetch, chunk_days=1)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    run = {"run_id": "r1", "rows_loaded": 0}
    pipeline.load_range(con, None, ds, start, start + timedelta(days=1), run, skip_loaded=True)
    pipeline.load_range(con, None, ds, start, start + timedelta(days=1), run, skip_loaded=True)
    assert len(calls) == 2  # an empty response is not "loaded": resume fetches it again
    assert con.execute(f"SELECT count(*) FROM {ds.table}").fetchone()[0] == 1


def test_optional_dataset_failure_is_recorded_not_raised(con):
    def down(client, a, b):
        raise RuntimeError("NESO portal down")

    run = {"run_id": "r1", "rows_loaded": 0}
    ds = replace(pipeline.DATASETS["neso_embedded_forecast"], fetch=down)
    assert pipeline.load_incremental(con, None, ds, NOW, 3, run) == 0
    assert "neso_embedded_forecast" in run["warnings"][0]
    with pytest.raises(RuntimeError):  # core datasets still fail the run
        pipeline.load_incremental(con, None, replace(INTENSITY, fetch=down), NOW, 3, run)


def test_snapshots_survive_a_neso_failure(con, monkeypatch):
    official = pd.DataFrame({
        "issued_at": pd.to_datetime([NOW], utc=True),
        "period_from": pd.to_datetime([NOW], utc=True),
        "period_to": pd.to_datetime([NOW + HALF_HOUR], utc=True),
        "forecast": [120], "intensity_index": ["moderate"],
    })  # fmt: skip
    monkeypatch.setattr(pipeline.ci, "fetch_official_forecast", lambda c, now: official)
    monkeypatch.setattr(
        pipeline.wx, "fetch_weather_forecast", lambda c, now: official.iloc[0:0][["issued_at"]]
    )

    def down(client):
        raise RuntimeError("NESO portal down")

    monkeypatch.setattr(pipeline.neso, "fetch_wind_14d_forecast", down)
    run = {"run_id": "r1", "rows_loaded": 0}
    assert pipeline.snapshot_forecasts(con, None, NOW, run) == 1
    assert "neso_wind_14d" in run["warnings"][0]
