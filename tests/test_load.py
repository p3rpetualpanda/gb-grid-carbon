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


def test_prepare_adds_new_columns_to_an_existing_table():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA ml")
    con.execute(  # the table as it was before yhat_base/base_version
        "CREATE TABLE ml.model_forecasts (issued_at TIMESTAMPTZ NOT NULL, period_from TIMESTAMPTZ "
        "NOT NULL, lead INTEGER NOT NULL, yhat DOUBLE, model_version VARCHAR, _ingested_at "
        "TIMESTAMPTZ NOT NULL, _run_id VARCHAR NOT NULL)"
    )
    con.execute("INSERT INTO ml.model_forecasts VALUES (?, ?, 1, 90, 'v3', ?, 'r0')", [T0, T0, T0])
    prepare(con)
    prepare(con)  # idempotent
    row = con.execute("SELECT yhat, yhat_base, base_version FROM ml.model_forecasts").fetchone()
    assert row == (90.0, None, None)  # old rows stay NULL: unknown, not a copy of yhat


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
    first = periods(0, 1, 1)
    first.loc[1, "actual"] = None  # not published yet
    upsert(con, TABLE, first, KEY, "r1")
    assert upsert(con, TABLE, first, KEY, "r2") == 0  # a null that stays null is not a change
    runs = con.execute(f"SELECT _run_id FROM {TABLE} ORDER BY period_from").df()["_run_id"]
    assert runs.tolist() == ["r1", "r1"]


def test_null_in_batch_keeps_the_stored_value(con):
    upsert(con, TABLE, periods(0, 1, 5), KEY, "r1")
    batch = periods(0, 1, 5)
    batch.loc[1, "actual"] = None  # e.g. the API re-serves a period with its actual missing
    assert upsert(con, TABLE, batch, KEY, "r2") == 0
    assert table(con)["actual"].tolist() == [5, 5]
    runs = con.execute(f"SELECT _run_id FROM {TABLE} ORDER BY period_from").df()["_run_id"]
    assert runs.tolist() == ["r1", "r1"]


def test_partial_null_weather_row_keeps_stored_variables_but_takes_new_ones(con):
    key = ("time", "point_id")
    row = {"time": T0, "point_id": "p1", "latitude": 51.0, "longitude": 0.0, "temperature_2m": 8.0,
           "wind_speed_100m": 30.0, "shortwave_radiation": 10.0, "cloud_cover": 50.0}  # fmt: skip
    upsert(con, "raw.weather_observed", pd.DataFrame([row]), key, "r1")
    update = row | {"wind_speed_100m": float("nan"), "temperature_2m": 9.5}
    assert upsert(con, "raw.weather_observed", pd.DataFrame([update]), key, "r2") == 1
    got = con.execute(
        "SELECT temperature_2m, wind_speed_100m, _run_id FROM raw.weather_observed"
    ).fetchone()
    assert got == (9.5, 30.0, "r2")  # the new value lands, the missing one keeps the old


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


def test_tracked_run_keeps_warnings_on_success(con):
    with tracked_run(con, "partial") as run:
        run.setdefault("warnings", []).append("neso_wind_forecast: RuntimeError: down")
    status, err = con.execute("SELECT status, error FROM meta.pipeline_runs").fetchone()
    assert status == "success" and "neso_wind_forecast" in err


def test_upsert_refuses_a_table_that_already_holds_duplicate_keys(con):
    upsert(con, TABLE, periods(0, 1, 5), KEY, "r1")
    con.execute(f"INSERT INTO {TABLE} SELECT * FROM {TABLE}")  # a past bug left two rows per key
    with pytest.raises(ValueError, match="duplicate"):
        upsert(con, TABLE, periods(0, 1, 6), KEY, "r2")
    assert len(table(con)) == 4  # rolled back, nothing lost
