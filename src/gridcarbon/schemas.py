"""Pydantic models mirroring the upstream JSON. Invalid data raises: never drop it silently."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gridcarbon.timeutils import HALF_HOUR

IntensityIndex = Literal["very low", "low", "moderate", "high", "very high"]


# --- Carbon Intensity API (api.carbonintensity.org.uk) -----------------------------------------


class _Period(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    period_from: datetime = Field(alias="from")
    period_to: datetime = Field(alias="to")

    @model_validator(mode="after")
    def _is_half_hour(self) -> _Period:
        if self.period_from.tzinfo is None:
            raise ValueError(f"period without timezone: {self.period_from!r}")
        if self.period_to - self.period_from != HALF_HOUR:
            raise ValueError(
                f"expected a 30-minute period, got {self.period_from}..{self.period_to}"
            )
        return self


class Intensity(BaseModel):
    forecast: int | None = None
    actual: int | None = None
    index: IntensityIndex | None = None


class FuelShare(BaseModel):
    fuel: str
    perc: float = Field(ge=0, le=100)


class IntensityPeriod(_Period):
    intensity: Intensity


class GenerationPeriod(_Period):
    generationmix: list[FuelShare]


class Region(BaseModel):
    regionid: int
    shortname: str
    dnoregion: str | None = None
    intensity: Intensity
    generationmix: list[FuelShare]


class RegionalPeriod(_Period):
    regions: list[Region]


class IntensityResponse(BaseModel):
    data: list[IntensityPeriod]


class GenerationResponse(BaseModel):
    data: list[GenerationPeriod]


class RegionalResponse(BaseModel):
    data: list[RegionalPeriod]


# --- Open-Meteo (open-meteo.com) ---------------------------------------------------------------


class OpenMeteoLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    latitude: float
    longitude: float
    utc_offset_seconds: int = 0
    hourly: dict[str, list[Any]]

    @field_validator("utc_offset_seconds")
    @classmethod
    def _must_be_utc(cls, v: int) -> int:
        if v != 0:
            raise ValueError("Open-Meteo responses must be requested in GMT/UTC")
        return v

    @model_validator(mode="after")
    def _aligned_arrays(self) -> OpenMeteoLocation:
        if "time" not in self.hourly:
            raise ValueError("hourly block has no 'time' array")
        n = len(self.hourly["time"])
        bad = [k for k, v in self.hourly.items() if len(v) != n]
        if bad:
            raise ValueError(f"hourly arrays misaligned with 'time': {bad}")
        return self
