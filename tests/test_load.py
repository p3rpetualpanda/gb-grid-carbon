from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from gridcarbon.load import loaded_windows, log_window, max_time, prepare, tracked_run, upsert
from gridcarbon.timeutils import HALF_HOUR

T0 = datetime(2024, 1, 1, tzinfo=UTC)
TABLE, KEY = "raw.intensity_national", ("period_from",)


def periods(first: int, last: int, actual: int) -> pd.DataFrame:
    starts = [T0 + i * HALF_HOUR for i in range(first, last + 1)]
    return pd.DataFrame({
        "period_from": pd.to_datetime(starts, utc=True),
        "period_to": pd.to_datetime([s + HALF_HOUR for s in starts], utc=True),
        "forecast": actual, "actual": actual, "intensity_index": "low",
    })  # fmt: skip


def table(con) -> pd.DataFrame:
    return con.execute(f"SELECT period_from, actual FROM {TABLE} ORDER BY 1").df()


def test_prepare_is_idempotent(con):
    prepare(con)
    prepare(con)


def test_upsert_twice_is_idempotent(con):
    upsert(con, TABLE, periods(0, 9, 1), KEY, "r1")
    upsert(con, TABLE, periods(0, 9, 1), KEY, "r2")
    assert len(table(con)) == 10


def test_overlapping_batch_replaces_only_matching_keys(con):
    upsert(con, TABLE, periods(0, 3, actual=1), KEY, "r1")
    upsert(con, TABLE, periods(2, 5, actual=2), KEY, "r2")  # late-arriving revisions
    df = table(con)
    assert len(df) == 6
    assert df["actual"].tolist() == [1, 1, 2, 2, 2, 2]


def test_empty_batch_never_deletes_history(con):
    upsert(con, TABLE, periods(0, 3, 1), KEY, "r1")
    assert upsert(con, TABLE, periods(0, 3, 1).iloc[0:0], KEY, "r2") == 0
    assert len(table(con)) == 4


def test_duplicate_keys_in_batch_keep_last(con):
    batch = pd.concat([periods(0, 0, 1), periods(0, 0, 7)])
    assert upsert(con, TABLE, batch, KEY, "r1") == 1
    assert table(con)["actual"].tolist() == [7]


def test_lineage_columns_are_filled(con):
    upsert(con, TABLE, periods(0, 0, 1), KEY, "run-abc")
    run_id, ingested = con.execute(f"SELECT _run_id, _ingested_at FROM {TABLE}").fetchone()
    assert run_id == "run-abc" and ingested is not None


def test_unchanged_rows_keep_their_first_lineage(con):
    upsert(con, TABLE, periods(0, 1, 1), KEY, "r1")
    batch = periods(0, 1, 1)
    batch.loc[1, "actual"] = None  # a null that stays null must also count as unchanged
    upsert(con, TABLE, batch, KEY, "r2")
    assert upsert(con, TABLE, batch, KEY, "r3") == 0
    runs = con.execute(f"SELECT _run_id FROM {TABLE} ORDER BY period_from").df()["_run_id"]
    assert runs.tolist() == ["r1", "r2"]


def test_unknown_table_rejected(con):
    with pytest.raises(ValueError):
        upsert(con, "raw.nope", periods(0, 0, 1), KEY, "r1")


def test_max_time(con):
    assert max_time(con, TABLE, "period_from") is None
    upsert(con, TABLE, periods(0, 4, 1), KEY, "r1")
    assert max_time(con, TABLE, "period_from") == T0 + 4 * HALF_HOUR


def test_tracked_run_records_success_and_failure(con):
    with tracked_run(con, "ok") as run:
        run["rows_loaded"] = 5
    with pytest.raises(RuntimeError), tracked_run(con, "boom"):
        raise RuntimeError("upstream down")
    rows = dict(con.execute("SELECT command, status FROM meta.pipeline_runs").fetchall())
    assert rows == {"ok": "success", "boom": "failed"}
    err = con.execute("SELECT error FROM meta.pipeline_runs WHERE command='boom'").fetchone()[0]
    assert "upstream down" in err


def test_loaded_windows_roundtrip(con):
    log_window(con, "r1", "intensity", T0, T0 + timedelta(days=31), 1488)
    assert loaded_windows(con, "intensity") == {(T0, T0 + timedelta(days=31))}
    assert loaded_windows(con, "generation") == set()
