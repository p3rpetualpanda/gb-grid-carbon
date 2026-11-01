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
