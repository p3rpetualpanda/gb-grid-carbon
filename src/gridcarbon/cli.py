"""Command line entry point: `gridcarbon --help`."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import typer

from gridcarbon.config import Settings, get_settings
from gridcarbon.http import ApiClient
from gridcarbon.load import connect, tracked_run, upsert, utcnow
from gridcarbon.pipeline import (
    BACKFILL_DEFAULT,
    DATASETS,
    INCREMENTAL_DEFAULT,
    incremental_window,
    load_range,
    snapshot_forecasts,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="GB Grid Carbon pipeline.")

DatasetOpt = Annotated[
    list[str] | None,
    typer.Option("--dataset", "-d", help=f"Repeatable. One of: {', '.join(DATASETS)}"),
]

STATUS_TABLES = {
    "raw.intensity_national": "period_from",
    "raw.generation_mix": "period_from",
    "raw.intensity_regional": "period_from",
    "raw.weather_observed": "time",
    "raw.weather_historical_forecast": "time",
    "raw.official_forecast_snapshots": "issued_at",
    "raw.weather_forecast_snapshots": "issued_at",
    "ml.model_forecasts": "issued_at",
}


def _setup() -> Settings:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return settings


def _client(settings: Settings) -> ApiClient:
    return ApiClient(timeout_s=settings.http_timeout_s, pause_s=settings.request_pause_s)


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _resolve(names: list[str] | None, default: tuple[str, ...]) -> list[str]:
    names = names or list(default)
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        raise typer.BadParameter(f"unknown dataset(s): {unknown}. Choose from {list(DATASETS)}")
    return names


@app.command("init-db")
def init_db() -> None:
    """Create schemas and raw tables (safe to re-run)."""
    settings = _setup()
    connect(settings).close()
    typer.echo(f"Warehouse ready ({settings.db_target}).")


@app.command()
def backfill(
    dataset: DatasetOpt = None,
    start: Annotated[str | None, typer.Option(help="UTC date/time. Default: earliest")] = None,
    end: Annotated[str | None, typer.Option(help="UTC date/time. Default: now")] = None,
    resume: Annotated[bool, typer.Option(help="Skip windows already loaded")] = True,
) -> None:
    """Load history window by window. Resumable: re-run after an interruption."""
    settings = _setup()
    names = _resolve(dataset, BACKFILL_DEFAULT)
    con = connect(settings)
    client = _client(settings)
    end_dt = _parse_utc(end) or utcnow()
    with tracked_run(con, f"backfill {' '.join(names)}") as run:
        for name in names:
            ds = DATASETS[name]
            start_dt = _parse_utc(start) or ds.earliest
            load_range(con, client, ds, start_dt, end_dt, run, skip_loaded=resume)
    typer.echo(f"Backfill finished: {run['rows_loaded']:,} rows (run {run['run_id']}).")


@app.command()
def incremental(
    dataset: DatasetOpt = None,
    lookback_days: Annotated[int | None, typer.Option(help="Override LOOKBACK_DAYS")] = None,
    snapshots: Annotated[bool, typer.Option(help="Also snapshot the live forecasts")] = True,
) -> None:
    """Bring every dataset up to date (what the scheduled pipeline runs)."""
    settings = _setup()
    names = _resolve(dataset, INCREMENTAL_DEFAULT)
    lookback = lookback_days if lookback_days is not None else settings.lookback_days
    con = connect(settings)
    client = _client(settings)
    now = utcnow()
    with tracked_run(con, "incremental") as run:
        for name in names:
            ds = DATASETS[name]
            start, end = incremental_window(con, ds, now, lookback)
            load_range(con, client, ds, start, end, run)
        if snapshots:
            snapshot_forecasts(con, client, now, run)
    typer.echo(f"Incremental finished: {run['rows_loaded']:,} rows (run {run['run_id']}).")


@app.command()
def predict() -> None:
    """Train on all history and write a 48h forecast to ml.model_forecasts (needs dbt marts)."""
    from gridcarbon.forecast import predict_live  # needs the optional [ml] extra

    settings = _setup()
    con = connect(settings)
    with tracked_run(con, "predict") as run:
        rows = predict_live(con, utcnow())
        run["rows_loaded"] = upsert(
            con, "ml.model_forecasts", rows, ("issued_at", "period_from"), run["run_id"]
        )
    typer.echo(f"Forecast issued at {rows['issued_at'].iloc[0]}: {len(rows)} half-hours.")


@app.command()
def status() -> None:
    """Row counts and time coverage per raw table, plus the latest runs."""
    settings = _setup()
    con = connect(settings)
    typer.echo(f"{'table':<36}{'rows':>12}  {'from':<25} {'to':<25}")
    for table, col in STATUS_TABLES.items():
        n, lo, hi = con.execute(f"SELECT count(*), min({col}), max({col}) FROM {table}").fetchone()
        typer.echo(f"{table:<36}{n:>12,}  {lo!s:<25} {hi!s:<25}")
    typer.echo("\nLatest runs:")
    for row in con.execute(
        "SELECT run_id, command, started_at, status, rows_loaded, error "
        "FROM meta.pipeline_runs ORDER BY started_at DESC LIMIT 5"
    ).fetchall():
        typer.echo("  " + " | ".join("" if v is None else str(v) for v in row))


if __name__ == "__main__":
    app()
