"""Live track record by model version: MAE and bias by lead band and by day, our model vs NESO.

Reads marts.fct_model_vs_official (forecasts issued in the same pipeline run, scored once the
actual is published). Read-only.

    python analysis/live_bias.py                       # local warehouse
    DB_TARGET=motherduck python analysis/live_bias.py  # the live warehouse (token from .env)
    python analysis/live_bias.py --since 2026-10-18    # only forecasts issued from that date
"""

from __future__ import annotations

import argparse
import sys

import duckdb
import pandas as pd

from gridcarbon import forecast as fc
from gridcarbon.config import get_settings

BANDS = ([0, 12, 24, 48, 96], ["0-6h", "6-12h", "12-24h", "24-48h"])  # lead in half-hours


def connect() -> duckdb.DuckDBPyConnection:
    """Read-only connection (gridcarbon.load.connect would create tables, which this never needs)."""
    s = get_settings()
    if s.db_target == "motherduck":
        if s.motherduck_token is None:
            sys.exit("DB_TARGET=motherduck but MOTHERDUCK_TOKEN is not set")
        token = s.motherduck_token.get_secret_value()
        con = duckdb.connect(f"md:{s.motherduck_database}", config={"motherduck_token": token})
    else:
        con = duckdb.connect(str(s.duckdb_path), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


def scored(con, since: str | None) -> pd.DataFrame:
    where = "f.actual_gco2_kwh is not null and f.neso_gco2_kwh is not null"
    params = []
    if since:
        where += " and f.issued_at >= ?::timestamptz"
        params.append(since)
    return con.execute(
        f"""
        select f.model_version, f.issued_at, f.period_from, f.lead,
               f.model_gco2_kwh as model, f.neso_gco2_kwh as neso, f.actual_gco2_kwh as y
        from marts.fct_model_vs_official as f
        where {where}
        """,
        params,
    ).df()


def summary(rows: pd.DataFrame, by) -> pd.DataFrame:
    err = rows.assign(model_err=rows["model"] - rows["y"], neso_err=rows["neso"] - rows["y"])
    g = err.groupby(by, observed=True)
    return pd.DataFrame(
        {
            "n": g.size(),
            "our_mae": g["model_err"].apply(lambda e: e.abs().mean()),
            "neso_mae": g["neso_err"].apply(lambda e: e.abs().mean()),
            "our_bias": g["model_err"].mean(),
            "neso_bias": g["neso_err"].mean(),
        }
    ).round(1)


def report(rows: pd.DataFrame) -> str:
    out = []
    for version, v in rows.groupby("model_version"):
        band = pd.cut(v["lead"], BANDS[0], labels=BANDS[1])
        day = v["period_from"].dt.tz_convert("Europe/London").dt.date.rename("target_day")
        try:
            diff, lo, hi = fc.paired_bootstrap(v, "model", "neso")
            gap = (f"Our MAE minus NESO's: {diff:+.1f} (95% interval {lo:+.1f} to {hi:+.1f}, "
                   "resampling 3-day blocks; below zero = we are better)")  # fmt: skip
        except ValueError as e:  # too few days for an honest interval
            gap = f"Our MAE minus NESO's: no interval yet ({e})"
        regret = fc.window_regret(v, ["model", "neso"], hours=3, full_horizon=True)
        out += [
            f"== {version}: {v['issued_at'].nunique()} forecasts, {len(v)} scored half-hours, "
            f"targets {day.min()} to {day.max()}",
            gap,
            f"Greenest 3 h window regret (fully scored issues only: {len(regret)}): "
            f"ours {regret['model'].mean():.1f}, NESO {regret['neso'].mean():.1f} gCO2/kWh",
            "",
            "By lead band:",
            summary(v, band.rename("lead_band")).to_string(),
            "",
            "By target day (UK) - check this before trusting any average:",
            summary(v, day).to_string(),
            "",
        ]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="only forecasts issued on or after this date (UTC)")
    rows = scored(connect(), parser.parse_args().since)
    if rows.empty:
        sys.exit("No scored forecasts yet.")
    print(report(rows))


if __name__ == "__main__":
    main()
