from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from gridcarbon.extract import neso

# Hand-written in the portal's published schema (shortcut: not yet recorded from the live API).


def package(*resources: tuple[str, str, bool]) -> dict:
    return {
        "result": {
            "resources": [{"id": i, "name": n, "datastore_active": a} for i, n, a in resources]
        }
    }


WIND_PACKAGE = package(
    ("wind-hist", "Historic Day Ahead Wind Forecasts", True),
    ("wind-now", "Day Ahead Wind Forecast", True),
    ("wind-bmu", "Day Ahead Wind BMU Forecast", True),
    ("wind-csv", "Some CSV", False),
)
EMBEDDED_PACKAGE = package(
    ("emb-now", "Embedded Wind and Solar Forecasts", True),
    ("emb-2023", "Embedded Solar and Wind Forecast Archive 2023", True),
    ("emb-2024", "Embedded Solar and Wind Forecast Archive 2024", True),
)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def wind_record(issued: str, target: str, mw: float) -> dict:
    return {
        "Forecast_Timestamp": issued,
        "Datetime_GMT": target,
        "Date": target[:10],
        "Settlement_period": 1,
        "Capacity": 15000,
        "Incentive_forecast": mw,
    }


def sql_response(records):
    return {"success": True, "result": {"records": records}}


@pytest.fixture(autouse=True)
def fresh_cache():
    neso._resource_cache.clear()


def test_parse_wind_keeps_window_and_horizon():
    start, end = utc(2024, 1, 1), utc(2024, 1, 2)
    df = neso.parse_wind(
        [
            wind_record("2024-01-01T09:00:00Z", "2024-01-02T00:00:00", 5000),
            wind_record("2024-01-01T09:00:00Z", "2024-01-06T00:00:00", 9999),  # > MAX_AHEAD
            wind_record("2024-01-02T09:00:00Z", "2024-01-03T00:00:00", 9999),  # issued after end
        ],
        start,
        end,
    )
    assert df.columns.tolist() == neso.WIND_COLUMNS
    assert len(df) == 1
    assert df.loc[0, "issued_at"] == utc(2024, 1, 1, 9)
    assert df.loc[0, "period_from"] == utc(2024, 1, 2)
    assert df.loc[0, "forecast_mw"] == 5000.0


def test_parse_wind_rejects_off_grid_targets():
    with pytest.raises(ValueError, match="half-hour"):
        neso.parse_wind(
            [wind_record("2024-01-01T09:00:00Z", "2024-01-01T12:10:00", 1)],
            utc(2024, 1, 1),
            utc(2024, 1, 2),
        )


def test_parse_embedded_builds_target_from_date_and_time():
    df = neso.parse_embedded(
        [
            {
                "Forecast_Datetime": "2024-03-01T08:30:00",
                "DATE_GMT": "2024-03-01T00:00:00",
                "TIME_GMT": "13:30",
                "SETTLEMENT_DATE": "2024-03-01T00:00:00",
                "SETTLEMENT_PERIOD": 28,
                "EMBEDDED_WIND_FORECAST": 1200,
                "EMBEDDED_WIND_CAPACITY": 6500,
                "EMBEDDED_SOLAR_FORECAST": 4100,
                "EMBEDDED_SOLAR_CAPACITY": 17000,
            }
        ],
        utc(2024, 3, 1),
        utc(2024, 3, 2),
    )
    assert df.loc[0, "period_from"] == utc(2024, 3, 1, 13, 30)
    assert df.loc[0, "issued_at"] == utc(2024, 3, 1, 8, 30)
    assert df.loc[0, "solar_mw"] == 4100.0


def test_empty_responses_give_empty_frames():
    assert neso.parse_wind([], utc(2024, 1, 1), utc(2024, 1, 2)).empty
    assert neso.parse_embedded([], utc(2024, 1, 1), utc(2024, 1, 2)).empty


@responses.activate
def test_fetch_wind_skips_bmu_files_and_dedupes_overlapping_resources(client):
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=WIND_PACKAGE)
    same = wind_record("2024-01-01T09:00:00Z", "2024-01-02T00:00:00", 5000)
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([same]))
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([same]))
    df = neso.fetch_wind_forecast(client, utc(2024, 1, 1), utc(2024, 1, 2))
    sqls = [parse_qs(urlparse(c.request.url).query).get("sql", [""])[0] for c in responses.calls]
    queried = [s for s in sqls if s]
    assert len(queried) == 2
    assert not any("wind-bmu" in s for s in queried)
    assert "\"Forecast_Timestamp\" >= '2024-01-01T00:00:00'" in queried[0]
    assert len(df) == 1


@responses.activate
def test_fetch_embedded_queries_only_overlapping_years(client):
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=EMBEDDED_PACKAGE)
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([]))
    neso.fetch_embedded_forecast(client, utc(2024, 6, 1), utc(2024, 6, 4))
    sqls = " ".join(c.request.url for c in responses.calls)
    assert "emb-2024" in sqls and "emb-now" in sqls
    assert "emb-2023" not in sqls


@responses.activate
def test_sql_paginates_until_a_short_page(client, monkeypatch):
    monkeypatch.setattr(neso, "PAGE_SIZE", 2)
    rec = wind_record("2024-01-01T09:00:00Z", "2024-01-02T00:00:00", 1)
    for page in ([rec, rec], [rec, rec], [rec]):
        responses.add(
            responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response(page)
        )
    rows = neso._sql_rows(client, "r", "Forecast_Timestamp", utc(2024, 1, 1), utc(2024, 1, 2))
    assert len(rows) == 5
    assert "OFFSET 4" in responses.calls[2].request.url.replace("+", " ").replace("%20", " ")
