"""Record small, real API responses into tests/fixtures/ so tests run offline and deterministically.

Run once (or when the upstream format changes):  python scripts/record_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
CI = "https://api.carbonintensity.org.uk"
POINTS = {"latitude": "57.48,51.51", "longitude": "-4.22,-0.13"}
WEATHER_VARS = "temperature_2m,wind_speed_100m,shortwave_radiation,cloud_cover"

NESO = "https://api.neso.energy/api/3/action/datastore_search_sql"
NESO_WIND_ARCHIVE = "7524ec65-f782-4258-aaf8-5b926c17b966"  # Historic Day Ahead Wind Forecasts
NESO_EMBEDDED_2024 = "06abd00a-ef6b-488b-9b6d-5e08fdc0c890"  # Embedded ... Archive 2024
NESO_WIND_14D = "93c3048e-1dab-4057-a2a9-417540583929"  # 14 Days Ahead Wind Forecast

REQUESTS: dict[str, tuple[str, dict | None]] = {
    "intensity.json": (f"{CI}/intensity/2024-01-01T00:00Z/2024-01-01T02:00Z", None),
    "generation.json": (f"{CI}/generation/2024-01-01T00:00Z/2024-01-01T01:00Z", None),
    "regional.json": (f"{CI}/regional/intensity/2024-01-01T00:00Z/2024-01-01T00:30Z", None),
    "official_forecast.json": (f"{CI}/intensity/2024-01-01T00:00Z/fw48h", None),
    "weather_archive.json": (
        "https://archive-api.open-meteo.com/v1/archive",
        POINTS
        | {
            "hourly": WEATHER_VARS,
            "timezone": "GMT",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        },
    ),
    # One day-ahead wind vintage, and the first half hours of one embedded vintage (around
    # midnight, where TIME_GMT 00:00 is settlement period 48 of the previous day).
    "neso_wind.json": (
        NESO,
        {
            "sql": f'SELECT * FROM "{NESO_WIND_ARCHIVE}" WHERE "Forecast_Timestamp" >= '
            "'2024-01-01' AND \"Forecast_Timestamp\" < '2024-01-02' ORDER BY \"_id\" LIMIT 6"
        },
    ),
    "neso_embedded.json": (
        NESO,
        {
            "sql": f'SELECT * FROM "{NESO_EMBEDDED_2024}" WHERE "Forecast_Datetime" >= '
            "'2024-01-01T21:00' AND \"Forecast_Datetime\" < '2024-01-01T22:00' "
            'AND "DATE_GMT" >= \'2024-01-01T23:00\' ORDER BY "_id" LIMIT 6'
        },
    ),
    # The 14-day wind file keeps only its latest forecast; its first rows start before issue.
    "neso_wind_14d.json": (
        NESO,
        {"sql": f'SELECT * FROM "{NESO_WIND_14D}" ORDER BY "Datetime" LIMIT 6'},
    ),
}


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, (url, params) in REQUESTS.items():
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        (FIXTURES / name).write_text(json.dumps(resp.json(), indent=1), encoding="utf-8")
        print(f"wrote {name} ({len(resp.content):,} bytes)")


if __name__ == "__main__":
    main()
