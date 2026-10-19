import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from gridcarbon.extract import neso

# Parser tests use responses recorded from the live API (scripts/record_fixtures.py); the
# fetch tests use small hand-written rows in the same shape.


def package(*resources: tuple[str, str, bool]) -> dict:
    return {
        "result": {
            "resources": [{"id": i, "name": n, "datastore_active": a} for i, n, a in resources]
        }
    }


WIND_PACKAGE = package(
    ("wind-hist", "Historic Day Ahead Wind Forecasts", True),
    # Live layout: the current file holds only the latest forecast, with no Forecast_Timestamp.
    ("wind-now", "Day Ahead Wind Forecast", True),
    ("wind-bmu", "Day Ahead Wind BMU Forecast", True),
    ("wind-csv", "Some CSV", False),
)
EMBEDDED_PACKAGE = package(
    ("emb-now", "Embedded Solar and Wind Forecast", True),  # no Forecast_Datetime
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


# Datastore fields per resource, as datastore_search(limit=0) reports them.
FIELDS = {
    "wind-hist": ["Datetime_GMT", "Incentive_forecast", "Capacity", "Forecast_Timestamp"],
    "wind-now": ["Datetime_GMT", "Incentive_forecast", "Capacity"],
    "emb-now": ["DATE_GMT", "TIME_GMT", "EMBEDDED_WIND_FORECAST"],
    "emb-2023": ["DATE_GMT", "TIME_GMT", "EMBEDDED_WIND_FORECAST", "Forecast_Datetime"],
    "emb-2024": ["DATE_GMT", "TIME_GMT", "EMBEDDED_WIND_FORECAST", "Forecast_Datetime"],
}


def add_fields(fields=FIELDS):
    def reply(request):
        rid = parse_qs(urlparse(request.url).query)["resource_id"][0]
        body = {"result": {"fields": [{"id": f} for f in ["_id", *fields[rid]]]}}
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, f"{neso.API_URL}/datastore_search", callback=reply)


def sql_queries() -> list[str]:
    sqls = [parse_qs(urlparse(c.request.url).query).get("sql", [""])[0] for c in responses.calls]
    return [s for s in sqls if s]


@pytest.fixture(autouse=True)
def fresh_cache():
    neso._resource_cache.clear()
    neso._fields_cache.clear()


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


def test_parse_wind_recorded_response(fixture_json):
    records = fixture_json("neso_wind.json")["result"]["records"]
    df = neso.parse_wind(records, utc(2024, 1, 1), utc(2024, 1, 2))
    assert len(df) == len(records) == 6
    # Datetime_GMT is the start of the half hour; numerics arrive as strings from SQL.
    assert df.loc[0, "issued_at"] == utc(2024, 1, 1, 8, 49, 23)
    assert df.loc[0, "period_from"] == utc(2024, 1, 2)
    assert df.loc[0, "forecast_mw"] == 13053.0
    assert df["capacity_mw"].dtype == "float64"


def test_parse_embedded_recorded_response_uses_period_end(fixture_json):
    records = fixture_json("neso_embedded.json")["result"]["records"]
    df = neso.parse_embedded(records, utc(2024, 1, 1), utc(2024, 1, 2))
    assert len(df) == 6
    # TIME_GMT 00:00 on 2 Jan is settlement period 48 of 1 Jan: the half hour from 23:30.
    assert df.loc[0, "period_from"] == utc(2024, 1, 1, 23, 30)
    assert df.loc[1, "period_from"] == utc(2024, 1, 2)
    assert df.loc[0, "issued_at"] == utc(2024, 1, 1, 21, 12)
    assert df.loc[0, "wind_mw"] == 3530.0


def test_parse_embedded_accepts_short_time_format():
    df = neso.parse_embedded(
        [
            {
                "Forecast_Datetime": "2026-07-05T07:58:52",
                "DATE_GMT": "2026-07-05T00:00:00",
                "TIME_GMT": "16:00",
                "EMBEDDED_WIND_FORECAST": 1320,
                "EMBEDDED_WIND_CAPACITY": 6417,
                "EMBEDDED_SOLAR_FORECAST": 7648,
                "EMBEDDED_SOLAR_CAPACITY": 22126,
            }
        ],
        utc(2026, 7, 5),
        utc(2026, 7, 6),
    )
    assert df.loc[0, "period_from"] == utc(2026, 7, 5, 15, 30)


def test_empty_responses_give_empty_frames():
    assert neso.parse_wind([], utc(2024, 1, 1), utc(2024, 1, 2)).empty
    assert neso.parse_embedded([], utc(2024, 1, 1), utc(2024, 1, 2)).empty


@responses.activate
def test_fetch_wind_queries_only_the_archive(client):
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=WIND_PACKAGE)
    add_fields()
    rec = wind_record("2024-01-01T09:00:00Z", "2024-01-02T00:00:00", 5000)
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([rec]))
    df = neso.fetch_wind_forecast(client, utc(2024, 1, 1), utc(2024, 1, 2))
    queried = sql_queries()
    assert len(queried) == 1
    assert '"wind-hist"' in queried[0]
    assert "\"Forecast_Timestamp\" >= '2024-01-01T00:00:00'" in queried[0]
    assert "interval '4 days'" in queried[0]
    assert len(df) == 1


@responses.activate
def test_fetch_fails_when_no_resource_has_vintages(client):
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=WIND_PACKAGE)
    add_fields(FIELDS | {"wind-hist": ["Datetime_GMT"]})
    with pytest.raises(ValueError, match="Forecast_Timestamp"):
        neso.fetch_wind_forecast(client, utc(2024, 1, 1), utc(2024, 1, 2))


@responses.activate
def test_fetch_embedded_queries_only_overlapping_years(client):
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=EMBEDDED_PACKAGE)
    add_fields()
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([]))
    neso.fetch_embedded_forecast(client, utc(2024, 6, 1), utc(2024, 6, 4))
    sqls = " ".join(sql_queries())
    assert "emb-2024" in sqls
    assert "emb-2023" not in sqls
    assert "emb-now" not in sqls  # current file: no Forecast_Datetime


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


def test_parse_wind_14d_recorded_response_drops_past_targets(fixture_json):
    records = fixture_json("neso_wind_14d.json")["result"]["records"]
    df = neso.parse_wind_14d(records)
    assert df.columns.tolist() == neso.WIND_COLUMNS
    # Issued 16:30 UTC; the file starts an hour earlier, and only 30 min before issue is kept.
    assert df["issued_at"].unique().tolist() == [utc(2026, 10, 18, 16, 30)]
    assert df.loc[0, "period_from"] == utc(2026, 10, 18, 16)
    assert len(df) == 5
    assert df.loc[0, "forecast_mw"] == 8671.0 and df["capacity_mw"].dtype == "float64"
    assert neso.parse_wind_14d([]).empty


@responses.activate
def test_fetch_wind_14d_reads_the_national_file_only(client):
    pkg = package(("w14", "14 Days Ahead Wind Forecast", True),
                  ("w14-bmu", "14 Day Ahead Wind BMU Forecast", True))  # fmt: skip
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=pkg)
    rec = {"ForecastDateTime": "2026-10-18T16:30:00", "Datetime": "2026-10-18T17:00:00",
           "Wind_Forecast": 9000, "Capacity": 25502}  # fmt: skip
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([rec]))
    df = neso.fetch_wind_14d_forecast(client)
    queried = sql_queries()
    assert len(queried) == 1 and '"w14"' in queried[0]
    assert df.loc[0, "forecast_mw"] == 9000.0


@responses.activate
def test_fetch_wind_14d_fails_if_the_layout_changes(client):
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=package())
    with pytest.raises(ValueError, match="14-day"):
        neso.fetch_wind_14d_forecast(client)


@responses.activate
def test_fetch_embedded_fails_when_a_year_has_no_archive(client):
    # Early January, before NESO publishes the new year's archive: loading only December's
    # vintages would leave a silent gap.
    responses.add(responses.GET, f"{neso.API_URL}/package_show", json=EMBEDDED_PACKAGE)
    add_fields()
    responses.add(responses.GET, f"{neso.API_URL}/datastore_search_sql", json=sql_response([]))
    with pytest.raises(ValueError, match="2025"):
        neso.fetch_embedded_forecast(client, utc(2024, 12, 30), utc(2025, 1, 2))
