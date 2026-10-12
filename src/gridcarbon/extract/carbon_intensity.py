"""Carbon Intensity API (NESO): national intensity, generation mix, regional data, 48h forecast.

Verified API behaviour (docs/data_dictionary.md):
- Documented max range per request: intensity 31 days, generation 60, regional 14 (else HTTP 400).
  For intensity and regional a span of *exactly* the limit is also rejected (the extra leading
  half-hour below tips it over), so we request whole-day windows strictly under the limit.
- A request for [from, to] returns periods whose *end* lies in [from, to], i.e. it also includes
  the period that finishes exactly at `from`. We filter to period_from in [from, to) so adjacent
  windows never double count.
- generation and regional silently stop at 31 Dec 23:00 when a request crosses a year boundary
  (HTTP 200, data just missing). Their windows are split at 1 January.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd

from gridcarbon.http import ApiClient
from gridcarbon.schemas import GenerationResponse, IntensityResponse, RegionalResponse
from gridcarbon.timeutils import chunk_range, ensure_utc, floor_half_hour, to_api

BASE_URL = "https://api.carbonintensity.org.uk"

NATIONAL_EARLIEST = datetime(2017, 9, 11, 23, 0, tzinfo=UTC)
MIX_EARLIEST = datetime(2018, 5, 10, 23, 30, tzinfo=UTC)

API_LIMIT_DAYS = {"intensity": 31, "generation": 60, "regional": 14}
INTENSITY_MAX_DAYS = 30
GENERATION_MAX_DAYS = 60
REGIONAL_MAX_DAYS = 13

INTENSITY_COLUMNS = ["period_from", "period_to", "forecast", "actual", "intensity_index"]
GENERATION_COLUMNS = ["period_from", "period_to", "fuel", "perc"]
REGIONAL_COLUMNS = [
    "period_from",
    "period_to",
    "region_id",
    "short_name",
    "dno_region",
    "forecast",
    "intensity_index",
    "generation_mix",
]
OFFICIAL_FORECAST_COLUMNS = [
    "issued_at",
    "period_from",
    "period_to",
    "forecast",
    "intensity_index",
]


def _frame(rows: list[dict], columns: list[str], key: list[str], ints: tuple[str, ...] = ()):
    df = pd.DataFrame(rows, columns=columns)
    for col in ints:
        df[col] = df[col].astype("Int64")
    for col in ("issued_at", "period_from", "period_to"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    return df.drop_duplicates(subset=key, keep="last").reset_index(drop=True)


def fetch_intensity(client: ApiClient, start: datetime, end: datetime) -> pd.DataFrame:
    """National half-hourly forecast + actual intensity (gCO2/kWh), period_from in [start, end)."""
    rows = []
    for a, b in chunk_range(start, end, INTENSITY_MAX_DAYS):
        payload = client.get_json(f"{BASE_URL}/intensity/{to_api(a)}/{to_api(b)}")
        for p in IntensityResponse.model_validate(payload).data:
            if a <= p.period_from < b:
                rows.append(
                    {
                        "period_from": p.period_from,
                        "period_to": p.period_to,
                        "forecast": p.intensity.forecast,
                        "actual": p.intensity.actual,
                        "intensity_index": p.intensity.index,
                    }
                )
    return _frame(rows, INTENSITY_COLUMNS, ["period_from"], ints=("forecast", "actual"))


def fetch_generation(client: ApiClient, start: datetime, end: datetime) -> pd.DataFrame:
    """National generation mix, long format: one row per period per fuel."""
    rows = []
    for a, b in chunk_range(start, end, GENERATION_MAX_DAYS, split_years=True):
        payload = client.get_json(f"{BASE_URL}/generation/{to_api(a)}/{to_api(b)}")
        for p in GenerationResponse.model_validate(payload).data:
            if a <= p.period_from < b:
                rows.extend(
                    {
                        "period_from": p.period_from,
                        "period_to": p.period_to,
                        "fuel": f.fuel,
                        "perc": f.perc,
                    }
                    for f in p.generationmix
                )
    return _frame(rows, GENERATION_COLUMNS, ["period_from", "fuel"])


def fetch_regional(client: ApiClient, start: datetime, end: datetime) -> pd.DataFrame:
    """Regional forecast intensity (no actuals upstream); mix kept as raw JSON for dbt to unnest."""
    rows = []
    for a, b in chunk_range(start, end, REGIONAL_MAX_DAYS, split_years=True):
        payload = client.get_json(f"{BASE_URL}/regional/intensity/{to_api(a)}/{to_api(b)}")
        for p in RegionalResponse.model_validate(payload).data:
            if not a <= p.period_from < b:
                continue
            for r in p.regions:
                rows.append(
                    {
                        "period_from": p.period_from,
                        "period_to": p.period_to,
                        "region_id": r.regionid,
                        "short_name": r.shortname,
                        "dno_region": r.dnoregion,
                        "forecast": r.intensity.forecast,
                        "intensity_index": r.intensity.index,
                        "generation_mix": json.dumps(
                            [f.model_dump() for f in r.generationmix], separators=(",", ":")
                        ),
                    }
                )
    return _frame(rows, REGIONAL_COLUMNS, ["period_from", "region_id"], ints=("forecast",))


def fetch_official_forecast(client: ApiClient, issued_at: datetime) -> pd.DataFrame:
    """Snapshot of NESO's 48h forward forecast as seen at `issued_at` (our benchmark).

    `issued_at` is the half-hour we fetched it in; NESO does not publish the exact issue time.
    """
    issued_at = floor_half_hour(ensure_utc(issued_at))
    payload = client.get_json(f"{BASE_URL}/intensity/{to_api(issued_at)}/fw48h")
    rows = [
        {
            "issued_at": issued_at,
            "period_from": p.period_from,
            "period_to": p.period_to,
            "forecast": p.intensity.forecast,
            "intensity_index": p.intensity.index,
        }
        for p in IntensityResponse.model_validate(payload).data
        if p.period_from >= issued_at
    ]
    return _frame(rows, OFFICIAL_FORECAST_COLUMNS, ["issued_at", "period_from"], ints=("forecast",))
