"""Open-Meteo weather for a fixed set of GB points (no API key; CC BY 4.0, non-commercial).

Three flavours, all hourly and requested in GMT (= UTC):
- observed (reanalysis archive, ~5 day lag): for analysis.
- historical *forecasts* (as issued): for model training, so training matches what is known at
  prediction time (training on observed weather would leak the future into backtests).
- live forecast: snapshot every run, used as model input at prediction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from gridcarbon.http import ApiClient
from gridcarbon.schemas import OpenMeteoLocation
from gridcarbon.timeutils import chunk_range, ensure_utc, floor_hour

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = ("temperature_2m", "wind_speed_100m", "shortwave_radiation", "cloud_cover")

OBSERVED_EARLIEST = datetime(2017, 9, 11, tzinfo=UTC)
HISTORICAL_FORECAST_EARLIEST = datetime(2022, 1, 1, tzinfo=UTC)
OBSERVED_MAX_DAYS = 366
HISTORICAL_FORECAST_MAX_DAYS = 92


@dataclass(frozen=True)
class WeatherPoint:
    point_id: str
    latitude: float
    longitude: float
    role: str  # why this point matters for the grid


WEATHER_POINTS: tuple[WeatherPoint, ...] = (
    WeatherPoint("north_scotland", 57.48, -4.22, "onshore wind + hydro"),
    WeatherPoint("south_scotland", 55.86, -4.25, "onshore wind, demand"),
    WeatherPoint("north_sea_offshore", 53.90, 1.80, "offshore wind (Dogger/Hornsea area)"),
    WeatherPoint("manchester", 53.48, -2.24, "demand"),
    WeatherPoint("birmingham", 52.49, -1.89, "demand"),
    WeatherPoint("london", 51.51, -0.13, "demand, solar"),
    WeatherPoint("south_england_solar", 51.06, -1.80, "solar farms"),
)

WEATHER_COLUMNS = ["time", "point_id", "latitude", "longitude", *HOURLY_VARS]


def _base_params(points: tuple[WeatherPoint, ...]) -> dict[str, str]:
    return {
        "latitude": ",".join(str(p.latitude) for p in points),
        "longitude": ",".join(str(p.longitude) for p in points),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "GMT",
    }


def parse_locations(payload, points: tuple[WeatherPoint, ...]) -> list[dict]:
    """Flatten Open-Meteo's per-location arrays into one row per (time, point)."""
    locations = payload if isinstance(payload, list) else [payload]
    if len(locations) != len(points):
        raise ValueError(f"expected {len(points)} locations, got {len(locations)}")
    rows = []
    for point, raw in zip(points, locations, strict=True):
        loc = OpenMeteoLocation.model_validate(raw)
        missing = [var for var in HOURLY_VARS if var not in loc.hourly]
        if missing:  # a renamed variable would otherwise load as NULLs the model quietly ignores
            raise ValueError(f"Open-Meteo response for {point.point_id} lacks {missing}")
        for i, t in enumerate(loc.hourly["time"]):
            row = {
                "time": datetime.fromisoformat(t).replace(tzinfo=UTC),
                "point_id": point.point_id,
                "latitude": point.latitude,
                "longitude": point.longitude,
            }
            for var in HOURLY_VARS:
                row[var] = loc.hourly[var][i]
            rows.append(row)
    return rows


def _frame(rows: list[dict], extra_key: tuple[str, ...] = ()) -> pd.DataFrame:
    cols = [*extra_key, *WEATHER_COLUMNS]
    df = pd.DataFrame(rows, columns=cols)
    for col in ("issued_at", "time"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    for var in HOURLY_VARS:
        df[var] = pd.to_numeric(df[var], errors="raise").astype("float64")
    # The archive returns nulls for the most recent days it has not processed yet: not data.
    df = df.dropna(subset=list(HOURLY_VARS), how="all")
    key = [*extra_key, "time", "point_id"]
    return df.drop_duplicates(subset=key, keep="last").reset_index(drop=True)


def _inclusive_end_date(end: datetime) -> date:
    # Open-Meteo end_date is inclusive; our windows are half-open [start, end).
    return (end - timedelta(seconds=1)).date()


def _fetch_range(
    client: ApiClient,
    url: str,
    start: datetime,
    end: datetime,
    max_days: int,
    points: tuple[WeatherPoint, ...],
) -> pd.DataFrame:
    rows = []
    for a, b in chunk_range(start, end, max_days):
        params = _base_params(points) | {
            "start_date": a.date().isoformat(),
            "end_date": _inclusive_end_date(b).isoformat(),
        }
        rows.extend(
            r for r in parse_locations(client.get_json(url, params), points) if a <= r["time"] < b
        )
    return _frame(rows)


def fetch_weather_observed(
    client: ApiClient,
    start: datetime,
    end: datetime,
    points: tuple[WeatherPoint, ...] = WEATHER_POINTS,
) -> pd.DataFrame:
    return _fetch_range(client, ARCHIVE_URL, start, end, OBSERVED_MAX_DAYS, points)


def fetch_weather_historical_forecast(
    client: ApiClient,
    start: datetime,
    end: datetime,
    points: tuple[WeatherPoint, ...] = WEATHER_POINTS,
) -> pd.DataFrame:
    return _fetch_range(
        client, HISTORICAL_FORECAST_URL, start, end, HISTORICAL_FORECAST_MAX_DAYS, points
    )


def fetch_weather_forecast(
    client: ApiClient, issued_at: datetime, points: tuple[WeatherPoint, ...] = WEATHER_POINTS
) -> pd.DataFrame:
    """Snapshot of the live forecast (next ~3 days) as seen at `issued_at`."""
    issued_at = floor_hour(ensure_utc(issued_at))
    params = _base_params(points) | {"forecast_days": 3}
    rows = [
        {"issued_at": issued_at, **r}
        for r in parse_locations(client.get_json(FORECAST_URL, params), points)
        if r["time"] >= issued_at
    ]
    return _frame(rows, extra_key=("issued_at",))
