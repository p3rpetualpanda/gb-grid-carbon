"""Orchestration: which datasets exist, how to backfill them, and how to keep them current."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from gridcarbon.extract import carbon_intensity as ci
from gridcarbon.extract import neso
from gridcarbon.extract import weather as wx
from gridcarbon.http import ApiClient
from gridcarbon.load import loaded_windows, log_window, max_time, upsert
from gridcarbon.timeutils import HALF_HOUR, chunk_range, ensure_utc, floor_half_hour

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Dataset:
    name: str
    table: str
    time_col: str
    key_cols: tuple[str, ...]
    earliest: datetime
    chunk_days: int
    fetch: Callable[[ApiClient, datetime, datetime], pd.DataFrame]


DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in (
        Dataset(
            "intensity", "raw.intensity_national", "period_from", ("period_from",),
            ci.NATIONAL_EARLIEST, ci.INTENSITY_MAX_DAYS, ci.fetch_intensity,
        ),
        Dataset(
            "generation", "raw.generation_mix", "period_from", ("period_from", "fuel"),
            ci.MIX_EARLIEST, ci.GENERATION_MAX_DAYS, ci.fetch_generation,
        ),
        Dataset(
            "regional", "raw.intensity_regional", "period_from", ("period_from", "region_id"),
            ci.MIX_EARLIEST, ci.REGIONAL_MAX_DAYS, ci.fetch_regional,
        ),
        Dataset(
            "weather_observed", "raw.weather_observed", "time", ("time", "point_id"),
            wx.OBSERVED_EARLIEST, wx.OBSERVED_MAX_DAYS, wx.fetch_weather_observed,
        ),
        Dataset(
            "weather_historical_forecast", "raw.weather_historical_forecast", "time",
            ("time", "point_id"), wx.HISTORICAL_FORECAST_EARLIEST,
            wx.HISTORICAL_FORECAST_MAX_DAYS, wx.fetch_weather_historical_forecast,
        ),
        # Keyed on issue time: every vintage is kept, so features can use the one known at issue.
        Dataset(
            "neso_wind_forecast", "raw.neso_wind_forecast", "issued_at",
            ("issued_at", "period_from"), neso.EARLIEST, neso.WIND_MAX_DAYS,
            neso.fetch_wind_forecast,
        ),
        Dataset(
            "neso_embedded_forecast", "raw.neso_embedded_forecast", "issued_at",
            ("issued_at", "period_from"), neso.EARLIEST, neso.EMBEDDED_MAX_DAYS,
            neso.fetch_embedded_forecast,
        ),
    )
}  # fmt: skip

# Regional history is ~3 MB per 10 days of JSON, so it is opt-in for backfills.
BACKFILL_DEFAULT = (
    "intensity",
    "generation",
    "weather_observed",
    "weather_historical_forecast",
    "neso_wind_forecast",
    "neso_embedded_forecast",
)
INCREMENTAL_DEFAULT = tuple(DATASETS)


def load_range(
    con: duckdb.DuckDBPyConnection,
    client: ApiClient,
    ds: Dataset,
    start: datetime,
    end: datetime,
    run: dict,
    *,
    skip_loaded: bool = False,
) -> int:
    """Fetch and upsert [start, end) window by window, logging each window to meta.load_log.

    With skip_loaded=True, windows already recorded in meta.load_log are skipped, so an interrupted
    backfill resumes where it stopped (window boundaries are deterministic for a given start).
    """
    start = max(ensure_utc(start), ds.earliest)
    end = ensure_utc(end)
    done = loaded_windows(con, ds.name) if skip_loaded else set()
    total = 0
    for a, b in chunk_range(start, end, ds.chunk_days):
        if (a, b) in done:
            log.debug("%s: skip %s..%s (already loaded)", ds.name, a, b)
            continue
        n = upsert(con, ds.table, ds.fetch(client, a, b), ds.key_cols, run["run_id"])
        log_window(con, run["run_id"], ds.name, a, b, n)
        log.info("%s: %s..%s -> %d rows", ds.name, a.date(), b.date(), n)
        total += n
        run["rows_loaded"] += n
    return total


def incremental_window(
    con: duckdb.DuckDBPyConnection, ds: Dataset, now: datetime, lookback_days: int
) -> tuple[datetime, datetime]:
    """From (latest loaded - lookback) to the end of the current half-hour.

    The lookback re-fetches recent periods whose `actual` values arrive late. Anchoring on the
    latest loaded row (not on `now`) means a pipeline that was down for weeks catches up.
    """
    now = ensure_utc(now)
    latest = max_time(con, ds.table, ds.time_col)
    anchor = latest if latest is not None else now
    start = max(ds.earliest, floor_half_hour(anchor - timedelta(days=lookback_days)))
    return start, floor_half_hour(now) + HALF_HOUR


def snapshot_forecasts(
    con: duckdb.DuckDBPyConnection, client: ApiClient, now: datetime, run: dict
) -> int:
    """Append the forecasts as they look right now. These cannot be re-fetched later."""
    official = ci.fetch_official_forecast(client, now)
    weather = wx.fetch_weather_forecast(client, now)
    n = upsert(
        con,
        "raw.official_forecast_snapshots",
        official,
        ("issued_at", "period_from"),
        run["run_id"],
    )
    n += upsert(
        con,
        "raw.weather_forecast_snapshots",
        weather,
        ("issued_at", "time", "point_id"),
        run["run_id"],
    )
    # Last: a NESO portal failure still fails the run, but after the other snapshots are saved.
    n += upsert(
        con,
        "raw.neso_wind_14d_snapshots",
        neso.fetch_wind_14d_forecast(client),
        ("issued_at", "period_from"),
        run["run_id"],
    )
    log.info("snapshots: %d rows", n)
    run["rows_loaded"] += n
    return n
