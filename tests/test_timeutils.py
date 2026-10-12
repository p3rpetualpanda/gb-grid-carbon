from datetime import UTC, datetime, timedelta, timezone

import pytest

from gridcarbon.timeutils import chunk_range, floor_half_hour, to_api


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_chunks_cover_range_without_gaps_or_overlaps():
    start, end = utc(2024, 1, 1), utc(2024, 3, 15)
    windows = list(chunk_range(start, end, 31))
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:], strict=False):
        assert prev_end == next_start
    assert all(b - a <= timedelta(days=31) for a, b in windows)
    assert len(windows) == 3


def test_split_years_cuts_at_new_year():
    # Regression: generation/regional endpoints silently drop data after 31 Dec in a request.
    windows = list(chunk_range(utc(2022, 12, 15), utc(2023, 2, 13), 60, split_years=True))
    assert windows == [
        (utc(2022, 12, 15), utc(2023, 1, 1)),
        (utc(2023, 1, 1), utc(2023, 2, 13)),
    ]


def test_exact_multiple_has_no_empty_tail():
    windows = list(chunk_range(utc(2024, 1, 1), utc(2024, 1, 1) + timedelta(days=62), 31))
    assert len(windows) == 2


def test_empty_when_start_not_before_end():
    assert list(chunk_range(utc(2024, 1, 2), utc(2024, 1, 1), 31)) == []
    assert list(chunk_range(utc(2024, 1, 1), utc(2024, 1, 1), 31)) == []


def test_rejects_naive_datetimes_and_bad_sizes():
    with pytest.raises(ValueError, match="naive"):
        list(chunk_range(datetime(2024, 1, 1), utc(2024, 1, 2), 31))  # noqa: DTZ001
    with pytest.raises(ValueError):
        list(chunk_range(utc(2024, 1, 1), utc(2024, 1, 2), 0))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (utc(2024, 6, 1, 10, 47, 12), utc(2024, 6, 1, 10, 30)),
        (utc(2024, 6, 1, 10, 29, 59), utc(2024, 6, 1, 10, 0)),
        (utc(2024, 6, 1, 10, 30), utc(2024, 6, 1, 10, 30)),
        # British Summer Time input is converted to UTC first.
        (
            datetime(2024, 6, 1, 11, 47, tzinfo=timezone(timedelta(hours=1))),
            utc(2024, 6, 1, 10, 30),
        ),
    ],
)
def test_floor_half_hour(value, expected):
    assert floor_half_hour(value) == expected


def test_to_api_format():
    assert to_api(utc(2024, 1, 5, 7, 30)) == "2024-01-05T07:30Z"
