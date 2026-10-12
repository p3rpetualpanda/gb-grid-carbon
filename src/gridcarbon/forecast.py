"""Phase 8: 48-hour forecast of national carbon intensity (direct multi-horizon LightGBM).

One model predicts every lead time (1..96 half-hours ahead) from what is known at issue time:
lead, the target's UK calendar, recent actuals (respecting their publication lag), the same slot
1-2 days and 1 week earlier, and *forecast* weather for the target hour. The leakage rules live in
`features()` and are pinned by tests/test_forecast.py.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

HORIZON = 96  # half-hours = 48 h
STEP = pd.Timedelta(minutes=30)
ACTUAL_LAG = pd.Timedelta(hours=1)  # actuals publish late: never use the last hour before issue
# 6.5 h = 13 half-hours, coprime with the 48 in a day, so training issues cycle through every
# half-hour of the day. Live runs issue at any half-hour; a fixed 6 h grid left (lead, slot)
# combinations unseen and skewed live forecasts.
ISSUE_EVERY = "390min"
MODEL_VERSION = "lgbm-direct-v1"
FEATURES = [
    "lead", "halfhour_slot", "iso_weekday", "month", "is_holiday",
    "last_actual", "mean_24h", "lag_day", "lag_week",
    "wind_speed", "solar_radiation", "temperature",
]  # fmt: skip
HOLIDAYS_CSV = Path(__file__).resolve().parents[2] / "dbt" / "seeds" / "uk_bank_holidays.csv"

# Same point roles as dbt/models/intermediate/int_weather_gb.sql, applied to forecast weather.
GB_WEATHER_SQL = """
    select time as hour_utc,
        avg(wind_speed_100m) filter (
            where point_id in ('north_scotland', 'south_scotland', 'north_sea_offshore')
        ) as wind_speed,
        avg(shortwave_radiation) filter (
            where point_id in ('london', 'south_england_solar')
        ) as solar_radiation,
        avg(temperature_2m) filter (
            where point_id in ('manchester', 'birmingham', 'london')
        ) as temperature
    from {table} {where}
    group by 1 order by 1
"""


def holidays() -> set:
    with HOLIDAYS_CSV.open(encoding="utf-8") as f:
        return {datetime.fromisoformat(r["holiday_date"]).date() for r in csv.DictReader(f)}


def load_history(con) -> tuple[pd.Series, pd.DataFrame]:
    """Actuals on the complete half-hour spine, NESO's published forecast, historical weather."""
    grid = (
        con.sql(
            "select period_from, actual_gco2_kwh as y, forecast_gco2_kwh as neso "
            "from marts.fct_grid_halfhourly order by 1"
        )
        .df()
        .set_index("period_from")
    )
    wx = con.sql(GB_WEATHER_SQL.format(table="raw.weather_historical_forecast", where="")).df()
    return grid, wx.set_index("hour_utc")


def issue_times(start, end) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start).ceil(ISSUE_EVERY), end, freq=ISSUE_EVERY)


def features(grid: pd.DataFrame, wx: pd.DataFrame, issues: pd.DatetimeIndex, hols: set):
    """One row per (issue, lead). Only data with period_from < issue - ACTUAL_LAG is used."""
    y = grid["y"]
    issued = issues.repeat(HORIZON)
    lead = np.tile(np.arange(1, HORIZON + 1), len(issues))
    target = issued + pd.to_timedelta((lead - 1) * 30, unit="min")
    last_known = issued - ACTUAL_LAG - STEP  # latest period whose actual may be used
    # Smallest whole number of days back from the target that lands on known data.
    days_back = np.ceil((target - last_known) / pd.Timedelta(days=1)).astype(int)
    local = target.tz_convert("Europe/London")
    weather = wx.reindex(target.floor("h"))
    return pd.DataFrame(
        {
            "issued_at": issued,
            "period_from": target,
            "lead": lead,
            "halfhour_slot": local.hour * 2 + local.minute // 30,
            "iso_weekday": local.dayofweek + 1,
            "month": local.month,
            "is_holiday": pd.Index(local.date).isin(hols).astype(int),
            "last_actual": y.ffill().reindex(last_known).to_numpy(),
            "mean_24h": y.rolling(48, min_periods=24).mean().reindex(last_known).to_numpy(),
            "lag_day": y.reindex(target - pd.to_timedelta(days_back, unit="D")).to_numpy(),
            "lag_week": y.reindex(target - pd.Timedelta(days=7)).to_numpy(),
            "wind_speed": weather["wind_speed"].to_numpy(),
            "solar_radiation": weather["solar_radiation"].to_numpy(),
            "temperature": weather["temperature"].to_numpy(),
            "y": y.reindex(target).to_numpy(),
            "neso": grid["neso"].reindex(target).to_numpy(),
        }
    )


def fit(rows: pd.DataFrame):
    import lightgbm as lgb  # optional dependency: pip install -e ".[ml]"

    rows = rows.dropna(subset=["y"])
    model = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.05, num_leaves=63, min_child_samples=50,
        subsample=0.8, subsample_freq=1, colsample_bytree=0.9, verbose=-1,
    )  # fmt: skip
    return model.fit(rows[FEATURES], rows["y"])


def training_rows(grid: pd.DataFrame, wx: pd.DataFrame, hols: set, until) -> pd.DataFrame:
    """Rows whose targets all lie before `until` (issues stop 48 h earlier)."""
    start = max(grid.index.min() + pd.Timedelta(days=8), wx.index.min())
    return features(grid, wx, issue_times(start, pd.Timestamp(until) - HORIZON * STEP), hols)


def backtest(grid: pd.DataFrame, wx: pd.DataFrame, hols: set, months: int = 12) -> pd.DataFrame:
    """Rolling-origin backtest: for each of the last `months` months, train only on targets
    before the month starts, then forecast every 6-hourly issue inside it."""
    last = grid["y"].dropna().index.max()
    folds = pd.date_range(end=last.floor("D"), periods=months + 1, freq="MS")
    out = []
    for fold_start, fold_end in zip(folds[:-1], folds[1:], strict=True):
        model = fit(training_rows(grid, wx, hols, fold_start))
        test = features(grid, wx, issue_times(fold_start, fold_end - STEP), hols)
        out.append(test.assign(model=model.predict(test[FEATURES])))
    return pd.concat(out, ignore_index=True).dropna(subset=["y"])


def predict_live(con, now: datetime) -> pd.DataFrame:
    """Train on all history, then forecast the next 48 h from the latest weather snapshot.

    shortcut: retrains on every pipeline run (~20 s), so no model artifact to store or version;
    persist the model if training time ever matters.
    """
    grid, wx_hist = load_history(con)
    hols = holidays()
    # Issue at the same time as this run's NESO snapshot, so the two forecasts join exactly.
    snap = con.sql("select max(issued_at) from raw.official_forecast_snapshots").fetchone()[0]
    issue = pd.Timestamp(snap if snap is not None else now).tz_convert(UTC).floor("30min")
    model = fit(training_rows(grid, wx_hist, hols, issue))
    wx_live = (
        con.sql(
            GB_WEATHER_SQL.format(
                table="raw.weather_forecast_snapshots",
                where="where issued_at = "
                "(select max(issued_at) from raw.weather_forecast_snapshots)",
            )
        )
        .df()
        .set_index("hour_utc")
    )
    rows = features(grid, wx_live.combine_first(wx_hist), pd.DatetimeIndex([issue]), hols)
    return pd.DataFrame(
        {
            "issued_at": rows["issued_at"],
            "period_from": rows["period_from"],
            "lead": rows["lead"],
            "yhat": model.predict(rows[FEATURES]),
            "model_version": MODEL_VERSION,
        }
    )


def mae_by_lead(preds: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Mean absolute error per lead-time band, for the model and each baseline."""
    bands = pd.cut(
        preds["lead"] * 0.5, [0, 6, 12, 24, 48], labels=["0-6h", "6-12h", "12-24h", "24-48h"]
    )
    complete = preds.dropna(subset=columns)  # compare every method on the same rows
    errs = {c: (complete[c] - complete["y"]).abs() for c in columns}
    table = pd.DataFrame(errs).groupby(bands[complete.index], observed=True).mean()
    table.loc["all"] = pd.DataFrame(errs).mean()
    return table
