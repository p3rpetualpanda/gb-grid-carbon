from datetime import UTC, datetime

import pytest
import responses

from gridcarbon.extract import weather as wx

TWO_POINTS = (
    wx.WeatherPoint("north_scotland", 57.48, -4.22, "wind"),
    wx.WeatherPoint("london", 51.51, -0.13, "demand"),
)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_parse_locations_flattens_per_point(fixture_json):
    rows = wx.parse_locations(fixture_json("weather_archive.json"), TWO_POINTS)
    assert len(rows) == 2 * 24
    assert {r["point_id"] for r in rows} == {"north_scotland", "london"}
    assert rows[0]["time"] == utc(2024, 1, 1)
    assert all(r["time"].tzinfo is UTC for r in rows)


def test_parse_locations_rejects_point_mismatch(fixture_json):
    with pytest.raises(ValueError, match="expected 1 locations"):
        wx.parse_locations(fixture_json("weather_archive.json"), TWO_POINTS[:1])


@responses.activate
def test_observed_window_is_half_open_and_end_date_inclusive(client, fixture_json):
    responses.add(responses.GET, wx.ARCHIVE_URL, json=fixture_json("weather_archive.json"))
    df = wx.fetch_weather_observed(client, utc(2024, 1, 1), utc(2024, 1, 1, 12), TWO_POINTS)
    assert len(df) == 12 * 2
    assert df["time"].max() == utc(2024, 1, 1, 11)
    assert "end_date=2024-01-01" in responses.calls[0].request.url


@responses.activate
def test_all_null_hours_are_dropped(client, fixture_json):
    payload = fixture_json("weather_archive.json")
    for loc in payload:
        for var in wx.HOURLY_VARS:
            loc["hourly"][var][-6:] = [None] * 6  # archive not yet processed
    responses.add(responses.GET, wx.ARCHIVE_URL, json=payload)
    df = wx.fetch_weather_observed(client, utc(2024, 1, 1), utc(2024, 1, 2), TWO_POINTS)
    assert len(df) == (24 - 6) * 2
