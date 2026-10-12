import pytest
from pydantic import ValidationError

from gridcarbon.schemas import (
    GenerationResponse,
    IntensityPeriod,
    IntensityResponse,
    OpenMeteoLocation,
    RegionalResponse,
)


def test_recorded_fixtures_parse(fixture_json):
    assert len(IntensityResponse.model_validate(fixture_json("intensity.json")).data) == 5
    gen = GenerationResponse.model_validate(fixture_json("generation.json")).data
    assert {f.fuel for f in gen[0].generationmix} >= {"gas", "wind", "nuclear", "solar"}
    regional = RegionalResponse.model_validate(fixture_json("regional.json")).data
    assert [r.regionid for r in regional[0].regions] == list(range(1, 19))


def _period(**overrides):
    payload = {
        "from": "2024-01-01T00:00Z",
        "to": "2024-01-01T00:30Z",
        "intensity": {"forecast": 100, "actual": 90, "index": "low"},
    }
    payload.update(overrides)
    return payload


def test_parses_api_timestamps_as_utc():
    p = IntensityPeriod.model_validate(_period())
    assert p.period_from.utcoffset().total_seconds() == 0
    assert p.period_from.hour == 0 and p.period_to.minute == 30


def test_actual_may_be_missing():
    p = IntensityPeriod.model_validate(_period(intensity={"forecast": 100, "index": "low"}))
    assert p.intensity.actual is None


def test_rejects_unknown_index():
    with pytest.raises(ValidationError):
        IntensityPeriod.model_validate(_period(intensity={"forecast": 1, "index": "extreme"}))


def test_rejects_non_half_hour_period():
    with pytest.raises(ValidationError, match="30-minute"):
        IntensityPeriod.model_validate(_period(to="2024-01-01T01:00Z"))


def test_rejects_impossible_fuel_share():
    bad = {"data": [{"from": "2024-01-01T00:00Z", "to": "2024-01-01T00:30Z",
                     "generationmix": [{"fuel": "gas", "perc": 140}]}]}  # fmt: skip
    with pytest.raises(ValidationError):
        GenerationResponse.model_validate(bad)


def test_open_meteo_requires_aligned_arrays_and_utc():
    good = {"latitude": 1, "longitude": 2, "utc_offset_seconds": 0,
            "hourly": {"time": ["2024-01-01T00:00"], "temperature_2m": [5.0]}}  # fmt: skip
    OpenMeteoLocation.model_validate(good)
    with pytest.raises(ValidationError, match="misaligned"):
        OpenMeteoLocation.model_validate(good | {"hourly": {"time": ["a", "b"], "x": [1]}})
    with pytest.raises(ValidationError, match="GMT"):
        OpenMeteoLocation.model_validate(good | {"utc_offset_seconds": 3600})
