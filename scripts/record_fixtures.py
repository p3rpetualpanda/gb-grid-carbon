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
