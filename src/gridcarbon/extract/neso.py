"""NESO Data Portal (api.neso.energy, CKAN): archived wind and solar forecasts, every vintage.

Two datasets, both half-hourly and both stamped with when each forecast was made, so a backtest
can use exactly the forecast that existed at issue time:
- Day-ahead wind forecast for transmission-connected wind (historic archive from 2018).
- 0-14 day embedded (distribution-connected) wind and solar forecasts (yearly archives from 2019).

Rows are pulled with `datastore_search_sql`, filtered on the forecast's own timestamp, so the
window-by-window backfill works on *issue* time. Only targets up to MAX_AHEAD after issue are
kept: the model forecasts 48 h, and 14-day embedded vintages would otherwise dominate storage.

Checked against the live API (Oct 2026):
- Only the archive files carry the issue timestamp; each dataset's "current" file (latest forecast
  only) has no such column, so resources are picked by their datastore fields, not their names.
- Embedded TIME_GMT is the *end* of the half hour (settlement period 48 is 00:00 the next day).
- Since mid-2026 the wind archive stamps each forecast 08:45 on the day it covers, so the night
  hours before that stamp are dropped as already past; this can only make the input later, never
  leak a later forecast.
The parsers fail loudly on surprises.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pandas as pd

from gridcarbon.http import ApiClient
from gridcarbon.timeutils import ensure_utc

API_URL = "https://api.neso.energy/api/3/action"
WIND_PACKAGE = "day-ahead-wind-forecast"
EMBEDDED_PACKAGE = "embedded-wind-and-solar-forecasts"

# Weather training data (Open-Meteo historical forecasts) starts in 2022, so earlier vintages
# could never be used as model inputs.
EARLIEST = datetime(2022, 1, 1, tzinfo=UTC)
WIND_MAX_DAYS = 7
EMBEDDED_MAX_DAYS = 3
MAX_AHEAD = timedelta(days=3)
PAGE_SIZE = 30_000  # CKAN caps rows per datastore_search_sql call (32,000 by default)

WIND_COLUMNS = ["issued_at", "period_from", "forecast_mw", "capacity_mw"]
EMBEDDED_COLUMNS = [
    "issued_at",
    "period_from",
    "wind_mw",
    "wind_capacity_mw",
    "solar_mw",
    "solar_capacity_mw",
]

_resource_cache: dict[str, list[dict]] = {}
_fields_cache: dict[str, set[str]] = {}


def _resources(client: ApiClient, package: str) -> list[dict]:
    """Datastore-backed resources of a dataset (resource ids change when NESO adds a year)."""
    if package not in _resource_cache:
        payload = client.get_json(f"{API_URL}/package_show", {"id": package})
        _resource_cache[package] = [
            r for r in payload["result"]["resources"] if r.get("datastore_active")
        ]
    return _resource_cache[package]


def _has_field(client: ApiClient, resource_id: str, field: str) -> bool:
    if resource_id not in _fields_cache:
        payload = client.get_json(
            f"{API_URL}/datastore_search", {"resource_id": resource_id, "limit": 0}
        )
        _fields_cache[resource_id] = {f["id"] for f in payload["result"]["fields"]}
    return field in _fields_cache[resource_id]


def _with_vintages(client: ApiClient, ids: list[str], ts_col: str) -> list[str]:
    """Keep resources that record when each forecast was made (the archives, not the current
    file). None left means NESO changed the layout: fail rather than load nothing."""
    kept = [i for i in ids if _has_field(client, i, ts_col)]
    if ids and not kept:
        raise ValueError(f"no NESO resource has a {ts_col} column")
    return kept


def _wind_resources(client: ApiClient) -> list[str]:
    # National forecasts only: the BMU (per wind farm) files share the dataset.
    ids = [r["id"] for r in _resources(client, WIND_PACKAGE) if "bmu" not in r["name"].lower()]
    return _with_vintages(client, ids, "Forecast_Timestamp")


def _embedded_resources(client: ApiClient, start: datetime, end: datetime) -> list[str]:
    """Yearly archives overlapping [start, end), plus any undated file that has vintages."""
    years = set(range(start.year, (end - timedelta(seconds=1)).year + 1))
    ids = []
    for r in _resources(client, EMBEDDED_PACKAGE):
        named = {int(y) for y in re.findall(r"\b(20\d\d)\b", r["name"])}
        if not named or named & years:
            ids.append(r["id"])
    return _with_vintages(client, ids, "Forecast_Datetime")


def _sql_rows(
    client: ApiClient,
    resource_id: str,
    ts_col: str,
    start: datetime,
    end: datetime,
    target_col: str | None = None,
):
    """Rows issued in [start, end). With `target_col`, targets beyond MAX_AHEAD (+1 day of
    slack for date-only columns) are cut server side: embedded vintages run 14 days ahead."""
    a, b = (ensure_utc(t).strftime("%Y-%m-%dT%H:%M:%S") for t in (start, end))
    horizon = ""
    if target_col:
        days = MAX_AHEAD.days + 1
        horizon = f'AND "{target_col}" < "{ts_col}" + interval \'{days} days\' '
    rows, offset = [], 0
    while True:
        sql = (
            f'SELECT * FROM "{resource_id}" WHERE "{ts_col}" >= \'{a}\' AND "{ts_col}" < \'{b}\' '
            f'{horizon}ORDER BY "{ts_col}", "_id" LIMIT {PAGE_SIZE} OFFSET {offset}'
        )
        page = client.get_json(f"{API_URL}/datastore_search_sql", {"sql": sql})["result"]["records"]
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def _utc(values) -> pd.Series:
    """Parse portal timestamps (with or without a zone; NESO's are GMT/UTC) as UTC."""
    ts = pd.to_datetime(pd.Series(values), utc=True, format="mixed")
    if ts.isna().any():
        raise ValueError("unparseable NESO timestamp")
    return ts


def _finish(df: pd.DataFrame, columns: list[str], start: datetime, end: datetime):
    if df.empty:
        return pd.DataFrame(columns=columns)
    if ((df["period_from"].dt.minute % 30) != 0).any() or (df["period_from"].dt.second != 0).any():
        raise ValueError("NESO forecast target not on a half-hour boundary")
    ahead = df["period_from"] - df["issued_at"]
    keep = (
        (df["issued_at"] >= start)
        & (df["issued_at"] < end)
        & (ahead >= -timedelta(minutes=30))
        & (ahead <= MAX_AHEAD)
    )
    df = df.loc[keep, columns]
    for col in columns[2:]:
        df[col] = pd.to_numeric(df[col], errors="raise").astype("float64")
    key = ["issued_at", "period_from"]
    return df.drop_duplicates(subset=key, keep="last").sort_values(key).reset_index(drop=True)


def parse_wind(records: list[dict], start: datetime, end: datetime) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=WIND_COLUMNS)
    raw = pd.DataFrame(records)
    df = pd.DataFrame(
        {
            "issued_at": _utc(raw["Forecast_Timestamp"]),
            "period_from": _utc(raw["Datetime_GMT"]),
            "forecast_mw": raw["Incentive_forecast"],
            "capacity_mw": raw["Capacity"],
        }
    )
    return _finish(df, WIND_COLUMNS, start, end)


def parse_embedded(records: list[dict], start: datetime, end: datetime) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=EMBEDDED_COLUMNS)
    raw = pd.DataFrame(records)
    # DATE_GMT may arrive as a date or a midnight timestamp; TIME_GMT is HH:MM or HH:MM:SS and
    # marks the end of the half hour (period 48 is 00:00 on the next DATE_GMT).
    day = pd.to_datetime(raw["DATE_GMT"], format="mixed").dt.strftime("%Y-%m-%d")
    period_end = _utc(day + "T" + raw["TIME_GMT"].astype(str).str[:5])
    df = pd.DataFrame(
        {
            "issued_at": _utc(raw["Forecast_Datetime"]),
            "period_from": period_end - timedelta(minutes=30),
            "wind_mw": raw["EMBEDDED_WIND_FORECAST"],
            "wind_capacity_mw": raw["EMBEDDED_WIND_CAPACITY"],
            "solar_mw": raw["EMBEDDED_SOLAR_FORECAST"],
            "solar_capacity_mw": raw["EMBEDDED_SOLAR_CAPACITY"],
        }
    )
    return _finish(df, EMBEDDED_COLUMNS, start, end)


def fetch_wind_forecast(client: ApiClient, start: datetime, end: datetime) -> pd.DataFrame:
    """Every day-ahead wind forecast vintage issued in [start, end)."""
    records = [
        row
        for rid in _wind_resources(client)
        for row in _sql_rows(client, rid, "Forecast_Timestamp", start, end, "Datetime_GMT")
    ]
    return parse_wind(records, start, end)


def fetch_embedded_forecast(client: ApiClient, start: datetime, end: datetime) -> pd.DataFrame:
    """Every embedded wind + solar forecast vintage issued in [start, end)."""
    records = [
        row
        for rid in _embedded_resources(client, start, end)
        for row in _sql_rows(client, rid, "Forecast_Datetime", start, end, "DATE_GMT")
    ]
    return parse_embedded(records, start, end)
