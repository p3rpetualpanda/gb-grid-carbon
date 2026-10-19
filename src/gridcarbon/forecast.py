"""Phase 8: 48-hour forecast of national carbon intensity (direct multi-horizon LightGBM).

One model predicts every lead time (1..96 half-hours ahead) from what is known at issue time:
lead, the target's UK calendar, recent actuals (respecting their publication lag), the same slot
1-2 days and 1 week earlier, and *forecast* weather for the target hour. The leakage rules live in
`features()` and are pinned by tests/test_forecast.py.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from gridcarbon.extract.weather import WIND_POINTS

HORIZON = 96  # half-hours = 48 h
STEP = pd.Timedelta(minutes=30)
ACTUAL_LAG = pd.Timedelta(hours=1)  # actuals publish late: never use the last hour before issue
# 6.5 h = 13 half-hours, coprime with the 48 in a day, so training issues cycle through every
# half-hour of the day. Live runs issue at any half-hour; a fixed 6 h grid left (lead, slot)
# combinations unseen and skewed live forecasts.
ISSUE_EVERY = "390min"
MODEL_VERSION = "lgbm-direct-v4"
# BASE_FEATURES only, when NESO or wind-point inputs are missing
FALLBACK_VERSION = "lgbm-direct-v4-fallback"
# Short leads lean on the last known actual: weight exp(-(lead-1)/6). Decay chosen on the first
# 6 backtest months, checked on the last 6 (0-2 h MAE 19.1 -> 17.0; overall unchanged).
PERSISTENCE_DECAY = 6
BASE_FEATURES = [
    "lead", "halfhour_slot", "iso_weekday", "month", "is_holiday",
    "last_actual", "mean_24h", "lag_day", "lag_week",
    "wind_speed", "solar_radiation", "temperature",
]  # fmt: skip
# Inputs from NESO's own forecasts (raw.neso_*), each the latest vintage made at least NESO_LAG
# before issue.
NESO_FEATURES = ["neso_wind_mw", "neso_embedded_wind_mw", "neso_embedded_solar_mw"]
# The live model (v3 on) uses the embedded forecasts but not transmission wind: NESO's day-ahead
# wind archive only covers the day it is published, so most 48 h targets have no vintage. Revisit
# once raw.neso_wind_14d_snapshots has a few weeks of history.
# Forecast wind speed at each wind point (extract/weather.py WIND_POINTS), separately: the GB
# average hides calm in one cluster while another blows. Live from v4.
WIND_POINT_FEATURES = [f"wind_{p}" for p in WIND_POINTS]
V3_FEATURES = BASE_FEATURES + ["neso_embedded_wind_mw", "neso_embedded_solar_mw"]
FEATURES = V3_FEATURES + WIND_POINT_FEATURES
NESO_LAG = pd.Timedelta(minutes=30)  # allowance for publication after Forecast_Timestamp
NESO_SQL = {
    "raw.neso_wind_forecast": "forecast_mw as neso_wind_mw",
    "raw.neso_embedded_forecast": "wind_mw as neso_embedded_wind_mw, "
    "solar_mw as neso_embedded_solar_mw",
}
log = logging.getLogger(__name__)
HOLIDAYS_CSV = Path(__file__).resolve().parents[2] / "dbt" / "seeds" / "uk_bank_holidays.csv"

# The three averages use the same point roles as dbt/models/intermediate/int_weather_gb.sql;
# then one wind-speed column per WIND_POINTS entry (WIND_POINT_FEATURES).
GB_WEATHER_SQL = (
    """
    select time as hour_utc,
        avg(wind_speed_100m) filter (
            where point_id in ('north_scotland', 'south_scotland', 'north_sea_offshore')
        ) as wind_speed,
        avg(shortwave_radiation) filter (
            where point_id in ('london', 'south_england_solar')
        ) as solar_radiation,
        avg(temperature_2m) filter (
            where point_id in ('manchester', 'birmingham', 'london')
        ) as temperature"""
    + "".join(
        f",\n        max(wind_speed_100m) filter (where point_id = '{p}') as wind_{p}"
        for p in WIND_POINTS
    )
    + """
    from {table} {where}
    group by 1 order by 1
"""
)


def holidays() -> set:
    with HOLIDAYS_CSV.open(encoding="utf-8") as f:
        return {datetime.fromisoformat(r["holiday_date"]).date() for r in csv.DictReader(f)}


def load_history(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(grid with actuals `y` + NESO forecast `neso` on the half-hour spine, hourly weather)."""
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


def load_neso_forecasts(con) -> list[pd.DataFrame]:
    """Every vintage of NESO's wind and embedded wind/solar forecasts (for `features(neso=)`)."""
    return [
        con.sql(f"select issued_at, period_from, {cols} from {table} order by 1, 2").df()
        for table, cols in NESO_SQL.items()
    ]


def latest_vintage(issued: pd.DatetimeIndex, target, vintages: pd.DataFrame) -> pd.DataFrame:
    """For each (issue, target) row: the newest vintage's values made by issue - NESO_LAG."""
    cols = [c for c in vintages.columns if c not in ("issued_at", "period_from")]
    left = pd.DataFrame(
        {
            "issued_at": (issued - NESO_LAG).astype("datetime64[ns, UTC]"),
            "period_from": pd.DatetimeIndex(target).astype("datetime64[ns, UTC]"),
            "_row": np.arange(len(issued)),
        }
    )
    right = vintages.astype(
        {"issued_at": "datetime64[ns, UTC]", "period_from": "datetime64[ns, UTC]"}
    )
    merged = pd.merge_asof(
        left.sort_values("issued_at"),
        right.sort_values("issued_at"),
        on="issued_at",
        by="period_from",
        direction="backward",
    )
    return merged.sort_values("_row")[cols].reset_index(drop=True)


def issue_times(start, end) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start).ceil(ISSUE_EVERY), end, freq=ISSUE_EVERY)


def features(
    grid: pd.DataFrame,
    wx: pd.DataFrame,
    issues: pd.DatetimeIndex,
    hols: set,
    neso: list[pd.DataFrame] | None = None,
):
    """One row per (issue, lead). Only data with period_from < issue - ACTUAL_LAG is used, and
    only NESO forecast vintages made by issue - NESO_LAG (NESO_FEATURES are NaN without `neso`).
    """
    y = grid["y"]
    issued = issues.repeat(HORIZON)
    lead = np.tile(np.arange(1, HORIZON + 1), len(issues))
    target = issued + pd.to_timedelta((lead - 1) * 30, unit="min")
    last_known = issued - ACTUAL_LAG - STEP  # latest period whose actual may be used
    # Smallest whole number of days back from the target that lands on known data.
    days_back = np.ceil((target - last_known) / pd.Timedelta(days=1)).astype(int)
    local = target.tz_convert("Europe/London")
    weather = wx.reindex(target.floor("h"))
    rows = pd.DataFrame(
        {
            "issued_at": issued,
            "period_from": target,
            "lead": lead,
            "halfhour_slot": local.hour * 2 + local.minute // 30,
            "iso_weekday": local.dayofweek + 1,
            "month": local.month,
            "is_holiday": pd.Index(local.date).isin(hols).astype(int),
            # Bridge up to 2 h of unpublished actuals; an older value is not "the last actual".
            "last_actual": y.ffill(limit=4).reindex(last_known).to_numpy(),
            "mean_24h": y.rolling(48, min_periods=24).mean().reindex(last_known).to_numpy(),
            "lag_day": y.reindex(target - pd.to_timedelta(days_back, unit="D")).to_numpy(),
            "lag_week": y.reindex(target - pd.Timedelta(days=7)).to_numpy(),
            "wind_speed": weather["wind_speed"].to_numpy(),
            "solar_radiation": weather["solar_radiation"].to_numpy(),
            "temperature": weather["temperature"].to_numpy(),
            **{c: weather[c].to_numpy() for c in WIND_POINT_FEATURES},
            "y": y.reindex(target).to_numpy(),
            "neso": grid["neso"].reindex(target).to_numpy(),
        }
    )
    for col in NESO_FEATURES:
        rows[col] = np.nan
    for vintages in neso or []:
        found = latest_vintage(issued, target, vintages)
        rows[found.columns] = found.to_numpy(dtype=float)
    return rows


def fit(rows: pd.DataFrame, feature_cols: list[str] = FEATURES):
    import lightgbm as lgb  # optional dependency: pip install -e ".[ml]"

    rows = rows.dropna(subset=["y"])
    model = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.05, num_leaves=63, min_child_samples=50,
        subsample=0.8, subsample_freq=1, colsample_bytree=0.9, verbose=-1,
    )  # fmt: skip
    return model.fit(rows[feature_cols], rows["y"])


def blend(rows: pd.DataFrame, model_pred) -> np.ndarray:
    """Model forecast, pulled towards the last known actual at short leads (when there is one)."""
    last = rows["last_actual"].to_numpy(dtype=float)
    w = np.where(np.isnan(last), 0.0, np.exp(-(rows["lead"].to_numpy() - 1) / PERSISTENCE_DECAY))
    return w * np.nan_to_num(last) + (1 - w) * np.asarray(model_pred)


def training_rows(
    grid: pd.DataFrame, wx: pd.DataFrame, hols: set, until, neso=None
) -> pd.DataFrame:
    """Rows whose targets all lie before `until` (issues stop 48 h earlier)."""
    start = max(grid.index.min() + pd.Timedelta(days=8), wx.index.min())
    issues = issue_times(start, pd.Timestamp(until) - HORIZON * STEP)
    return features(grid, wx, issues, hols, neso)


def backtest(
    grid: pd.DataFrame,
    wx: pd.DataFrame,
    hols: set,
    months: int = 12,
    neso: list[pd.DataFrame] | None = None,
    variants: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """Rolling-origin backtest: for each of the last `months` months, train only on targets
    before the month starts, then forecast every issue (each 6.5 h) inside it.

    `variants` maps an output column to a feature list (default: {"model": FEATURES}); every
    variant is trained and scored on the same folds, so they compare like for like."""
    variants = variants or {"model": FEATURES}
    last = grid["y"].dropna().index.max()
    folds = pd.date_range(end=last.floor("D"), periods=months + 1, freq="MS")
    out = []
    for fold_start, fold_end in zip(folds[:-1], folds[1:], strict=True):
        train = training_rows(grid, wx, hols, fold_start, neso)
        test = features(grid, wx, issue_times(fold_start, fold_end - STEP), hols, neso)
        for name, cols in variants.items():
            test[name] = blend(test, fit(train, cols).predict(test[cols]))
        out.append(test)
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
    neso = load_neso_forecasts(con)
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
    rows = features(grid, wx_live.combine_first(wx_hist), pd.DatetimeIndex([issue]), hols, neso)
    # A stalled NESO feed or a wind point missing from the snapshot leaves inputs that training
    # almost never saw: fall back to BASE_FEATURES, and label it, rather than publish v4 on them.
    cols, version = FEATURES, MODEL_VERSION
    if rows[FEATURES].drop(columns=BASE_FEATURES).isna().any().any():
        cols, version = BASE_FEATURES, FALLBACK_VERSION
    train = training_rows(grid, wx_hist, hols, issue, neso)
    # Training rows before NESO's archives start have NaN inputs while live rows never do: log
    # the share covered so a thin history shows up in the pipeline log.
    covered = train[FEATURES].drop(columns=BASE_FEATURES).notna().all(axis=1).mean()
    log.info("forecast %s: %.0f%% of training rows have NESO and wind-point inputs", version,
             100 * covered)  # fmt: skip
    model = fit(train, cols)
    yhat = blend(rows, model.predict(rows[cols]))
    return pd.DataFrame(
        {
            "issued_at": rows["issued_at"],
            "period_from": rows["period_from"],
            "lead": rows["lead"],
            "yhat": yhat,
            "model_version": version,
            # Stage-1 output and version. Equal to yhat/model_version until a correction step
            # exists; stored separately so its errors are never computed from corrected forecasts.
            "yhat_base": yhat,
            "base_version": version,
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


def paired_bootstrap(
    preds: pd.DataFrame,
    a: str,
    b: str,
    n: int = 1000,
    seed: int = 0,
    block_days: int = 3,
    min_days: int = 45,  # 15 blocks: fewer make a lumpy, falsely tight percentile interval
) -> tuple[float, float, float]:
    """MAE(a) - MAE(b) on rows where both exist, with a 95% interval from resampling blocks of
    `block_days` consecutive UK days of targets. Errors move together within a day and weather
    regimes last several days, so half-hours (or single days) aren't independent draws.
    Negative means `a` is better; trust it only if the whole interval is below zero.
    Raises ValueError with fewer than `min_days` days: the interval would be falsely tight."""
    rows = preds.dropna(subset=["y", a, b])
    day = pd.to_datetime(rows["period_from"].dt.tz_convert("Europe/London").dt.date)
    if day.nunique() < min_days:
        raise ValueError(f"{day.nunique()} days with y, {a} and {b}; need {min_days}")
    by_block = pd.DataFrame(
        {
            "gap": (rows[a] - rows["y"]).abs() - (rows[b] - rows["y"]).abs(),
            "block": (day - day.min()).dt.days // block_days,
        }
    ).groupby("block")["gap"]
    gap, count = by_block.sum().to_numpy(), by_block.size().to_numpy()
    blocks = len(count)
    draws = np.random.default_rng(seed).multinomial(blocks, np.full(blocks, 1 / blocks), size=n)
    lo, hi = np.percentile(draws @ gap / (draws @ count), [2.5, 97.5])
    return float(gap.sum() / count.sum()), float(lo), float(hi)


def window_regret(
    preds: pd.DataFrame, cols: list[str], hours: float = 3, full_horizon: bool = False
) -> pd.DataFrame:
    """Per issue and forecast column: how much dirtier (gCO2/kWh) the greenest `hours` window
    picked from that forecast actually was than the truly greenest one (0 = picked the best).
    Every column is scored on the same windows, those where all `cols` and the actual are
    complete, so a patchier forecast isn't judged on an easier set. Issues with no such window
    are dropped. `full_horizon` also drops issues scored on fewer leads than the most complete
    one (live issues whose later actuals aren't published yet), so groups compare like for like."""
    slots = max(1, round(hours * 2))
    rows = preds.dropna(subset=["y", *cols])
    if full_horizon:
        n = rows.groupby("issued_at")["lead"].transform("size")
        rows = rows[n == n.max()]

    def window_means(c: str) -> pd.DataFrame:  # issue x window-end lead; NaN if incomplete
        wide = rows.pivot(index="issued_at", columns="lead", values=c)
        return wide.reindex(columns=range(1, HORIZON + 1)).T.rolling(slots).mean().T

    means = {c: window_means(c) for c in ["y", *cols]}
    complete = np.logical_and.reduce([m.notna() for m in means.values()])
    keep = complete.any(axis=1)
    y_mean = means["y"].where(complete)[keep]
    best = y_mean.min(axis=1).to_numpy()
    out = {}
    for c in cols:
        chosen = y_mean.columns.get_indexer(means[c].where(complete)[keep].idxmin(axis=1))
        out[c] = y_mean.to_numpy()[np.arange(len(y_mean)), chosen] - best
    return pd.DataFrame(out, index=y_mean.index)
