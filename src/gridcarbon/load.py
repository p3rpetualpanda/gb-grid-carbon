"""Warehouse access: DuckDB locally, MotherDuck in production (same SQL, one env var apart).

Raw tables mirror the APIs (only snake_case renames) plus lineage columns. Loads are idempotent
upserts by natural key: rows in the batch replace any existing rows with the same key, inside one
transaction. Rows the batch does not mention are never deleted, so a partial or empty API response
cannot wipe history.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import duckdb
import pandas as pd

from gridcarbon.config import Settings

log = logging.getLogger(__name__)

_WEATHER_VARS = """
    temperature_2m DOUBLE, wind_speed_100m DOUBLE, shortwave_radiation DOUBLE, cloud_cover DOUBLE
"""
_LINEAGE = "_ingested_at TIMESTAMPTZ NOT NULL, _run_id VARCHAR NOT NULL"

DDL: dict[str, str] = {
    "raw.intensity_national": f"""
        period_from TIMESTAMPTZ NOT NULL, period_to TIMESTAMPTZ NOT NULL,
        forecast INTEGER, actual INTEGER, intensity_index VARCHAR, {_LINEAGE}""",
    "raw.generation_mix": f"""
        period_from TIMESTAMPTZ NOT NULL, period_to TIMESTAMPTZ NOT NULL,
        fuel VARCHAR NOT NULL, perc DOUBLE, {_LINEAGE}""",
    "raw.intensity_regional": f"""
        period_from TIMESTAMPTZ NOT NULL, period_to TIMESTAMPTZ NOT NULL,
        region_id INTEGER NOT NULL, short_name VARCHAR, dno_region VARCHAR,
        forecast INTEGER, intensity_index VARCHAR, generation_mix VARCHAR, {_LINEAGE}""",
    "raw.official_forecast_snapshots": f"""
        issued_at TIMESTAMPTZ NOT NULL, period_from TIMESTAMPTZ NOT NULL,
        period_to TIMESTAMPTZ NOT NULL, forecast INTEGER, intensity_index VARCHAR, {_LINEAGE}""",
    "raw.weather_observed": f"""
        time TIMESTAMPTZ NOT NULL, point_id VARCHAR NOT NULL, latitude DOUBLE, longitude DOUBLE,
        {_WEATHER_VARS}, {_LINEAGE}""",
    "raw.weather_historical_forecast": f"""
        time TIMESTAMPTZ NOT NULL, point_id VARCHAR NOT NULL, latitude DOUBLE, longitude DOUBLE,
        {_WEATHER_VARS}, {_LINEAGE}""",
    "raw.weather_forecast_snapshots": f"""
        issued_at TIMESTAMPTZ NOT NULL, time TIMESTAMPTZ NOT NULL, point_id VARCHAR NOT NULL,
        latitude DOUBLE, longitude DOUBLE, {_WEATHER_VARS}, {_LINEAGE}""",
    "meta.pipeline_runs": """
        run_id VARCHAR NOT NULL, command VARCHAR NOT NULL, started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ, status VARCHAR NOT NULL, rows_loaded BIGINT, error VARCHAR""",
    "meta.load_log": """
        run_id VARCHAR NOT NULL, dataset VARCHAR NOT NULL, window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL, rows_loaded BIGINT NOT NULL,
        loaded_at TIMESTAMPTZ NOT NULL""",
}


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    if settings.db_target == "motherduck":
        if settings.motherduck_token is None:
            raise RuntimeError("DB_TARGET=motherduck but MOTHERDUCK_TOKEN is not set")
        con = duckdb.connect(
            "md:", config={"motherduck_token": settings.motherduck_token.get_secret_value()}
        )
        # Connecting straight to a missing md:<db> hangs instead of erroring, so create it first.
        con.execute(f"CREATE DATABASE IF NOT EXISTS {settings.motherduck_database}")
        con.execute(f"USE {settings.motherduck_database}")
    else:
        settings.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(settings.duckdb_path))
    prepare(con)
    return con


def prepare(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("SET TimeZone = 'UTC'")
    for schema in ("raw", "meta"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for table, columns in DDL.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")


def utcnow() -> datetime:
    return datetime.now(UTC)


def upsert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    df: pd.DataFrame,
    key_cols: Iterable[str],
    run_id: str,
) -> int:
    """Replace rows sharing a natural key with the batch. Returns rows written."""
    if table not in DDL:
        raise ValueError(f"unknown table {table}")
    key_cols = list(key_cols)
    if df.empty:
        log.info("upsert %s: empty batch, nothing to do", table)
        return 0
    batch = df.drop_duplicates(subset=key_cols, keep="last").assign(
        _ingested_at=utcnow(), _run_id=run_id
    )
    match = " AND ".join(f"t.{c} = b.{c}" for c in key_cols)
    con.register("_batch", batch)
    try:
        con.begin()
        con.execute(f"DELETE FROM {table} AS t USING _batch AS b WHERE {match}")
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _batch")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.unregister("_batch")
    return len(batch)


def max_time(con: duckdb.DuckDBPyConnection, table: str, col: str) -> datetime | None:
    value = con.execute(f"SELECT max({col}) FROM {table}").fetchone()[0]
    return value.astimezone(UTC) if value is not None else None


# --- run bookkeeping ---------------------------------------------------------------------------


@contextmanager
def tracked_run(con: duckdb.DuckDBPyConnection, command: str) -> Iterator[dict]:
    """Record a pipeline run in meta.pipeline_runs, including failures (then re-raise)."""
    run = {"run_id": uuid.uuid4().hex[:12], "rows_loaded": 0}
    con.execute(
        "INSERT INTO meta.pipeline_runs (run_id, command, started_at, status) VALUES (?, ?, ?, ?)",
        [run["run_id"], command, utcnow(), "running"],
    )
    try:
        yield run
    except BaseException as exc:
        _finish_run(con, run, "failed", f"{type(exc).__name__}: {exc}"[:2000])
        raise
    _finish_run(con, run, "success", None)


def _finish_run(con, run: dict, status: str, error: str | None) -> None:
    con.execute(
        "UPDATE meta.pipeline_runs SET finished_at = ?, status = ?, rows_loaded = ?, error = ? "
        "WHERE run_id = ?",
        [utcnow(), status, run["rows_loaded"], error, run["run_id"]],
    )


def log_window(con, run_id: str, dataset: str, start: datetime, end: datetime, rows: int) -> None:
    con.execute(
        "INSERT INTO meta.load_log VALUES (?, ?, ?, ?, ?, ?)",
        [run_id, dataset, start, end, rows, utcnow()],
    )


def loaded_windows(con, dataset: str) -> set[tuple[datetime, datetime]]:
    rows = con.execute(
        "SELECT DISTINCT window_start, window_end FROM meta.load_log WHERE dataset = ?", [dataset]
    ).fetchall()
    return {(a.astimezone(UTC), b.astimezone(UTC)) for a, b in rows}
