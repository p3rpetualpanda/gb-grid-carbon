import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import responses

from gridcarbon.extract import carbon_intensity as ci
from gridcarbon.timeutils import HALF_HOUR


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def _parse_api_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


def fake_intensity_api(request):
    """Mimic the real API: return every period whose *end* lies in [from, to]."""
    frm, to = (_parse_api_time(s) for s in request.url.rsplit("/", 2)[-2:])
    data, end = [], frm
    while end <= to:
        data.append(
            {
                "from": f"{end - HALF_HOUR:%Y-%m-%dT%H:%MZ}",
                "to": f"{end:%Y-%m-%dT%H:%MZ}",
                "intensity": {"forecast": 100, "actual": 99, "index": "moderate"},
            }
        )
        end += HALF_HOUR
    return 200, {}, json.dumps({"data": data})


@responses.activate
def test_intensity_keeps_only_requested_window(client, fixture_json):
    responses.add(
        responses.GET,
        f"{ci.BASE_URL}/intensity/2024-01-01T00:00Z/2024-01-01T02:00Z",
        json=fixture_json("intensity.json"),
    )
    df = ci.fetch_intensity(client, utc(2024, 1, 1), utc(2024, 1, 1, 2))
    # The fixture also contains the 23:30 period (it *ends* at 00:00); it must be dropped.
    assert list(df.columns) == ci.INTENSITY_COLUMNS
    assert len(df) == 4
    assert df["period_from"].min() == utc(2024, 1, 1)
    assert str(df["actual"].dtype) == "Int64"


@responses.activate
def test_intensity_chunks_respect_31_day_limit(client):
    responses.add(responses.GET, re.compile(rf"{ci.BASE_URL}/intensity/.*"), json={"data": []})
    df = ci.fetch_intensity(client, utc(2024, 1, 1), utc(2024, 3, 15))
    assert df.empty and list(df.columns) == ci.INTENSITY_COLUMNS
    urls = [c.request.url for c in responses.calls]
    assert len(urls) == 3
    assert urls[0].endswith("/intensity/2024-01-01T00:00Z/2024-01-31T00:00Z")


@pytest.mark.parametrize(
    ("fetch", "path", "limit_days"),
    [
        (ci.fetch_intensity, "intensity", ci.API_LIMIT_DAYS["intensity"]),
        (ci.fetch_regional, "regional/intensity", ci.API_LIMIT_DAYS["regional"]),
    ],
)
@responses.activate
def test_requests_stay_strictly_under_api_limit(client, fetch, path, limit_days):
    # Regression: the live API rejects a span of *exactly* 31 (or 14) days with HTTP 400.
    responses.add(responses.GET, re.compile(rf"{ci.BASE_URL}/{path}/.*"), json={"data": []})
    fetch(client, utc(2024, 1, 1), utc(2024, 6, 1))
    for call in responses.calls:
        frm, to = (_parse_api_time(s) for s in call.request.url.rsplit("/", 2)[-2:])
        assert to - frm < timedelta(days=limit_days)


@responses.activate
def test_adjacent_chunks_never_duplicate_or_skip_periods(client):
    responses.add_callback(responses.GET, re.compile(rf"{ci.BASE_URL}/intensity/.*"),
                           callback=fake_intensity_api)  # fmt: skip
    start, days = utc(2024, 1, 1), 40  # spans two API chunks (30 + 10 days)
    df = ci.fetch_intensity(client, start, start + timedelta(days=days))
    assert len(responses.calls) == 2
    assert len(df) == days * 48
    assert df["period_from"].is_unique
    assert df["period_from"].min() == start
    assert df["period_from"].max() == start + timedelta(days=days) - HALF_HOUR


@responses.activate
def test_generation_is_long_format(client, fixture_json):
    responses.add(
        responses.GET,
        f"{ci.BASE_URL}/generation/2024-01-01T00:00Z/2024-01-01T01:00Z",
        json=fixture_json("generation.json"),
    )
    df = ci.fetch_generation(client, utc(2024, 1, 1), utc(2024, 1, 1, 1))
    assert len(df) == 2 * 9  # two half-hours x nine fuels
    assert not df.duplicated(["period_from", "fuel"]).any()
    sums = df.groupby("period_from")["perc"].sum()
    assert ((sums > 98) & (sums < 102)).all()


@responses.activate
def test_regional_keeps_mix_as_json(client, fixture_json):
    responses.add(
        responses.GET,
        f"{ci.BASE_URL}/regional/intensity/2024-01-01T00:00Z/2024-01-01T00:30Z",
        json=fixture_json("regional.json"),
    )
    df = ci.fetch_regional(client, utc(2024, 1, 1), utc(2024, 1, 1, 0, 30))
    assert len(df) == 18
    assert set(df["region_id"]) == set(range(1, 19))
    mix = json.loads(df.loc[df["short_name"] == "North Scotland", "generation_mix"].iloc[0])
    assert {m["fuel"] for m in mix} >= {"wind", "hydro"}


@responses.activate
def test_official_forecast_snapshot(client, fixture_json):
    responses.add(
        responses.GET,
        f"{ci.BASE_URL}/intensity/2024-01-01T00:00Z/fw48h",
        json=fixture_json("official_forecast.json"),
    )
    df = ci.fetch_official_forecast(client, utc(2024, 1, 1, 0, 10))  # floored to 00:00
    assert len(df) == 96  # 48 hours of half-hours
    assert (df["issued_at"] == utc(2024, 1, 1)).all()
    assert df["period_from"].min() == utc(2024, 1, 1)
