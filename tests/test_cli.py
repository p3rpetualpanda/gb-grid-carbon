import duckdb
import pytest
from typer.testing import CliRunner

from gridcarbon import cli
from gridcarbon.load import prepare


@pytest.fixture
def runner(monkeypatch):
    con = duckdb.connect()
    prepare(con)
    monkeypatch.setattr(cli, "_setup", lambda: cli.get_settings())
    monkeypatch.setattr(cli, "connect", lambda settings: con)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    return CliRunner()


def test_incremental_snapshots_even_if_a_load_fails(runner, monkeypatch):
    # Snapshots can't be re-fetched later; loads catch up through the look-back window.
    calls = []

    def boom(*args, **kwargs):
        raise RuntimeError("regional is down")

    monkeypatch.setattr(cli, "load_incremental", boom)
    monkeypatch.setattr(cli, "snapshot_forecasts", lambda *a, **k: calls.append("snapshot"))
    result = runner.invoke(cli.app, ["incremental"])
    assert result.exit_code != 0
    assert calls == ["snapshot"]


def test_incremental_still_loads_when_a_snapshot_fails(runner, monkeypatch):
    # An Open-Meteo outage must not stall the actuals; the run still ends failed.
    loaded = []

    def boom(*args, **kwargs):
        raise RuntimeError("open-meteo is down")

    monkeypatch.setattr(cli, "snapshot_forecasts", boom)
    monkeypatch.setattr(cli, "load_incremental", lambda *a, **k: loaded.append(a[2].name))
    result = runner.invoke(cli.app, ["incremental"])
    assert result.exit_code != 0
    assert loaded


def test_a_snapshot_failure_is_logged_even_if_a_load_fails_too(runner, monkeypatch, caplog):
    # Otherwise the load's error is raised and the snapshot's disappears.
    def snap_boom(*args, **kwargs):
        raise RuntimeError("open-meteo is down")

    def load_boom(*args, **kwargs):
        raise RuntimeError("regional is down")

    monkeypatch.setattr(cli, "snapshot_forecasts", snap_boom)
    monkeypatch.setattr(cli, "load_incremental", load_boom)
    runner.invoke(cli.app, ["incremental"])
    assert "open-meteo is down" in caplog.text


def add_run(con, started: str, error: str | None, command: str = "incremental") -> None:
    con.execute(
        "INSERT INTO meta.pipeline_runs (run_id, command, started_at, status, error) "
        "VALUES (?, ?, ?::timestamptz, 'success', ?)",
        [started, command, started, error],
    )


def test_check_warnings_fails_when_a_dataset_warns_in_every_recent_run(runner):
    con = cli.connect(None)
    for day in (1, 2, 3):
        add_run(con, f"2026-11-0{day}", "neso_embedded_forecast: ValueError: layout; other: X")
    result = runner.invoke(cli.app, ["check-warnings", "--runs", "3"])
    assert result.exit_code != 0
    assert "neso_embedded_forecast" in result.output


def test_check_warnings_passes_after_one_clean_run(runner):
    con = cli.connect(None)
    add_run(con, "2026-11-01", "neso_embedded_forecast: ValueError: layout")
    add_run(con, "2026-11-02", None)
    add_run(con, "2026-11-03", "neso_embedded_forecast: ValueError: layout")
    assert runner.invoke(cli.app, ["check-warnings", "--runs", "3"]).exit_code == 0


def test_check_warnings_needs_a_full_window_of_runs(runner):
    con = cli.connect(None)
    add_run(con, "2026-11-01", "neso_embedded_forecast: ValueError: layout")
    assert runner.invoke(cli.app, ["check-warnings", "--runs", "3"]).exit_code == 0
