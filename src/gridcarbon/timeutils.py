"""UTC time helpers. Everything in the pipeline is timezone-aware UTC; naive datetimes are bugs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

HALF_HOUR = timedelta(minutes=30)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed (use UTC): {dt!r}")
    return dt.astimezone(UTC)


def floor_half_hour(dt: datetime) -> datetime:
    dt = ensure_utc(dt)
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def floor_hour(dt: datetime) -> datetime:
    return ensure_utc(dt).replace(minute=0, second=0, microsecond=0)


def to_api(dt: datetime) -> str:
    """Format for Carbon Intensity API paths, e.g. 2024-01-01T00:00Z."""
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%MZ")


def chunk_range(
    start: datetime, end: datetime, max_days: int, split_years: bool = False
) -> Iterator[tuple[datetime, datetime]]:
    """Split [start, end) into contiguous half-open windows of at most `max_days`.

    Windows never overlap and never leave gaps, so per-window loads cover the range exactly once.
    split_years=True also cuts at 1 January (some endpoints silently truncate at year end).
    """
    if max_days <= 0:
        raise ValueError("max_days must be positive")
    start, end = ensure_utc(start), ensure_utc(end)
    step = timedelta(days=max_days)
    cursor = start
    while cursor < end:
        window_end = min(cursor + step, end)
        if split_years:
            window_end = min(window_end, datetime(cursor.year + 1, 1, 1, tzinfo=UTC))
        yield cursor, window_end
        cursor = window_end
