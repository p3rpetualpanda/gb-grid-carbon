import numpy as np
import pandas as pd
import pytest

from gridcarbon import forecast as fc

START = pd.Timestamp("2024-01-01", tz="UTC")


def spine(days: int, values=None) -> pd.DataFrame:
    idx = pd.date_range(START, periods=days * 48, freq="30min")
    y = np.arange(len(idx), dtype=float) if values is None else values(idx)
    return pd.DataFrame({"y": y, "neso": y}, index=idx)


def hourly_weather(days: int) -> pd.DataFrame:
    idx = pd.date_range(START, periods=days * 24, freq="h")
    rng = np.random.default_rng(0)
    wind = rng.uniform(0, 60, len(idx))
    return pd.DataFrame(
        {"wind_speed": wind, "solar_radiation": 0.0, "temperature": 10.0}
        | dict.fromkeys(fc.WIND_POINT_FEATURES, wind),
        index=idx,
    )


def test_weather_sql_has_a_wind_column_per_wind_point():
    import duckdb

    con = duckdb.connect()
    con.execute(
        "create table w (time timestamptz, point_id varchar, wind_speed_100m double, "
        "shortwave_radiation double, temperature_2m double)"
    )
    t = START.to_pydatetime()
    con.executemany(
        "insert into w values (?, ?, ?, 0, 10)",
        [(t, "dogger_bank_offshore", 30.0), (t, "north_scotland", 12.0)],
    )
    wx = con.sql(fc.GB_WEATHER_SQL.format(table="w", where="")).df().set_index("hour_utc")
    assert set(fc.WIND_POINT_FEATURES) <= set(wx.columns)
    # Each point's own speed, not an average; points with no row stay NULL rather than 0.
    assert wx["wind_dogger_bank_offshore"].iloc[0] == 30.0
    assert wx["wind_north_scotland"].iloc[0] == 12.0
    assert pd.isna(wx["wind_thames_offshore"].iloc[0])


def test_wind_points_are_pinned():
    # WIND_POINTS comes from role strings; pin it so a role edit can't quietly change model inputs.
    expected = (
        "north_scotland", "south_scotland", "north_sea_offshore", "moray_firth_offshore",
        "forth_offshore", "dogger_bank_offshore", "irish_sea_offshore", "lincs_offshore",
        "east_anglia_offshore", "thames_offshore", "southern_uplands",
    )  # fmt: skip
    assert [f"wind_{p}" for p in expected] == fc.WIND_POINT_FEATURES
    assert not set(fc.WIND_POINT_FEATURES) & set(fc.FEATURES)


def test_features_never_use_data_after_issue_minus_lag():
    # y = position on the spine, so a feature's value says exactly which period it came from.
    grid = spine(20)
    issue = START + pd.Timedelta(days=10, hours=13)
    rows = fc.features(grid, hourly_weather(20), pd.DatetimeIndex([issue]), set())
    newest_allowed = grid.index.get_loc(issue - fc.ACTUAL_LAG - fc.STEP)
    assert len(rows) == fc.HORIZON
    for col in ("last_actual", "mean_24h", "lag_day", "lag_week"):
        assert rows[col].max() <= newest_allowed, col
    assert (rows["period_from"] >= issue).all()
    # Targets are the next 96 half-hours and labels line up with them.
    assert rows["y"].tolist() == list(
        range(grid.index.get_loc(issue), grid.index.get_loc(issue) + 96)
    )


def test_lag_day_is_the_most_recent_known_same_slot():
    grid = spine(20)
    issue = START + pd.Timedelta(days=10)
    rows = fc.features(grid, hourly_weather(20), pd.DatetimeIndex([issue]), set()).set_index("lead")
    # lead 1 targets `issue`; one day back is known, so lag_day comes from exactly 24 h earlier.
    assert rows.loc[1, "lag_day"] == rows.loc[1, "y"] - 48
    # lead 96 targets issue + 47.5 h; two days back is issue - 30 min, still inside the 1 h
    # publication lag (unknown at issue time), so it must reach three days back.
    assert rows.loc[96, "lag_day"] == rows.loc[96, "y"] - 144


def test_last_actual_is_not_carried_over_a_long_publication_gap():
    issue = START + pd.Timedelta(days=10)
    last_known = issue - fc.ACTUAL_LAG - fc.STEP
    for gap_h, carried in ((1, True), (6, False)):
        grid = spine(20)
        grid.loc[last_known - pd.Timedelta(hours=gap_h) + fc.STEP : last_known, "y"] = np.nan
        rows = fc.features(grid, hourly_weather(20), pd.DatetimeIndex([issue]), set())
        # A short gap reuses the latest actual; a 6 h outage must not pass as the current value.
        assert rows["last_actual"].notna().all() == carried, gap_h


def test_blend_trusts_last_actual_at_lead_1_and_model_far_out():
    rows = pd.DataFrame({"lead": [1, 96, 1], "last_actual": [88.0, 88.0, np.nan]})
    out = fc.blend(rows, [170.0, 170.0, 170.0])
    assert out[0] == 88  # lead 1: the last known actual
    assert out[1] == pytest.approx(170, abs=0.01)  # 48 h out: the model
    assert out[2] == 170  # no known actual: the model


def test_training_issues_cover_every_half_hour_of_the_day():
    # Regression: a fixed 6 h issue grid never trained on live issue times like 13:30.
    issues = fc.issue_times(START, START + pd.Timedelta(days=30))
    assert set(issues.hour * 2 + issues.minute // 30) == set(range(48))


def test_backtest_trains_only_on_the_past(monkeypatch):
    seen = []

    def fake_fit(rows, feature_cols=fc.FEATURES):
        seen.append(rows["period_from"].max())

        class M:
            def predict(self, X):
                return np.zeros(len(X))

        return M()

    monkeypatch.setattr(fc, "fit", fake_fit)
    grid = spine(120)
    preds = fc.backtest(grid, hourly_weather(120), set(), months=2)
    fold_starts = sorted(preds.groupby(preds["issued_at"].dt.strftime("%Y-%m"))["issued_at"].min())
    for trained_until, fold_start in zip(seen, fold_starts, strict=True):
        assert trained_until < fold_start


def test_model_learns_a_daily_pattern():
    pytest.importorskip("lightgbm")
    grid = spine(
        60, lambda idx: 200 + 80 * np.sin(2 * np.pi * (idx.hour * 2 + idx.minute // 30) / 48)
    )
    wx = hourly_weather(60)
    model = fc.fit(fc.training_rows(grid, wx, set(), START + pd.Timedelta(days=50)))
    test = fc.features(grid, wx, pd.DatetimeIndex([START + pd.Timedelta(days=55)]), set())
    mae = np.abs(model.predict(test[fc.FEATURES]) - test["y"]).mean()
    assert mae < 10


def vintages(issued_at, period_from, value):
    return pd.DataFrame(
        {
            "issued_at": pd.to_datetime(issued_at, utc=True),
            "period_from": pd.to_datetime(period_from, utc=True),
            "neso_wind_mw": value,
        }
    )


def test_neso_features_use_only_vintages_made_before_issue_minus_lag():
    grid = spine(20)
    issue = START + pd.Timedelta(days=10)
    target = issue + pd.Timedelta(hours=5)
    neso = vintages(
        [issue - pd.Timedelta(hours=6), issue - fc.NESO_LAG, issue - pd.Timedelta(minutes=10)],
        [target] * 3,
        [1.0, 2.0, 3.0],  # 3.0 was made inside the publication lag: not yet known at issue
    )
    rows = fc.features(grid, hourly_weather(20), pd.DatetimeIndex([issue]), set(), [neso])
    by_target = rows.set_index("period_from")
    assert by_target.loc[target, "neso_wind_mw"] == 2.0
    # Targets no vintage covers stay missing (LightGBM handles NaN), never borrowed from others.
    assert by_target["neso_wind_mw"].drop(target).isna().all()
    assert rows["neso_embedded_solar_mw"].isna().all()


def test_neso_features_are_missing_without_neso_data():
    grid = spine(20)
    issue = pd.DatetimeIndex([START + pd.Timedelta(days=10)])
    rows = fc.features(grid, hourly_weather(20), issue, set())
    assert rows[fc.NESO_FEATURES].isna().all().all()


def test_backtest_scores_every_variant_on_the_same_rows(monkeypatch):
    def fake_fit(rows, feature_cols=fc.FEATURES):
        class M:
            def predict(self, X):
                return np.full(len(X), float(len(feature_cols)))

        return M()

    monkeypatch.setattr(fc, "fit", fake_fit)
    grid = spine(120)
    variants = {"model": fc.BASE_FEATURES, "model_neso": fc.BASE_FEATURES + fc.NESO_FEATURES}
    preds = fc.backtest(grid, hourly_weather(120), set(), months=1, variants=variants)
    far = preds[preds["lead"] == fc.HORIZON]  # blend weight ~0: the raw model output
    assert far["model"].round(2).eq(len(fc.BASE_FEATURES)).all()
    assert far["model_neso"].round(2).eq(len(fc.BASE_FEATURES) + 3).all()


def live_setup(monkeypatch, covered: int):
    """In-memory warehouse issuing at day 10, with an embedded vintage covering `covered` targets.
    Returns (con, issue, dict that receives the feature frame the model predicts on)."""
    import duckdb

    issue = START + pd.Timedelta(days=10)
    con = duckdb.connect()
    con.execute("create schema raw")
    con.execute("create table raw.official_forecast_snapshots (issued_at timestamptz)")
    con.execute("insert into raw.official_forecast_snapshots values (?)", [issue])
    con.execute(
        "create table raw.weather_forecast_snapshots (issued_at timestamptz, time timestamptz, "
        "point_id varchar, wind_speed_100m double, shortwave_radiation double, "
        "temperature_2m double)"
    )
    targets = pd.date_range(issue, periods=covered, freq="30min")
    embedded = pd.DataFrame(
        {
            "issued_at": issue - pd.Timedelta(hours=1),
            "period_from": targets,
            "neso_embedded_wind_mw": 1500.0,
            "neso_embedded_solar_mw": 200.0,
        }
    )
    monkeypatch.setattr(fc, "load_history", lambda con: (spine(20), hourly_weather(20)))
    monkeypatch.setattr(fc, "load_neso_forecasts", lambda con: [embedded])
    seen = {}

    def fake_fit(rows, feature_cols=fc.FEATURES):
        class M:
            def predict(self, X):
                seen["X"] = X
                return np.zeros(len(X))

        return M()

    monkeypatch.setattr(fc, "fit", fake_fit)
    return con, issue, seen


def test_predict_live_uses_neso_embedded_forecasts(monkeypatch):
    con, issue, seen = live_setup(monkeypatch, covered=fc.HORIZON)
    out = fc.predict_live(con, issue.to_pydatetime())
    assert len(out) == fc.HORIZON
    assert (out["model_version"] == fc.MODEL_VERSION).all()
    # Raw stage-1 output and its version, stored now so a later correction step can't hide them.
    assert out["yhat_base"].equals(out["yhat"]) and out["base_version"].equals(out["model_version"])
    assert "neso_wind_mw" not in seen["X"]  # transmission wind is mostly missing upstream
    assert (seen["X"]["neso_embedded_wind_mw"] == 1500.0).all()
    assert (seen["X"]["neso_embedded_solar_mw"] == 200.0).all()


def test_predict_live_falls_back_without_a_full_neso_vintage(monkeypatch):
    # A stalled NESO feed leaves later targets uncovered: use the model without NESO inputs and
    # say so in model_version, rather than publish a v3 forecast on inputs it never trained on.
    con, issue, seen = live_setup(monkeypatch, covered=40)
    out = fc.predict_live(con, issue.to_pydatetime())
    assert list(seen["X"].columns) == fc.BASE_FEATURES
    assert (out["model_version"] == fc.FALLBACK_VERSION).all()
    assert (out["base_version"] == fc.FALLBACK_VERSION).all()


def scored_preds(days: int = 60, seed: int = 0) -> pd.DataFrame:
    """Synthetic backtest output: one issue per day, 48 half-hourly targets each, with errors
    that share a per-day offset (as real forecast errors do)."""
    rng = np.random.default_rng(seed)
    issued = pd.date_range(START, periods=days, freq="D")
    rows = pd.DataFrame({
        "issued_at": issued.repeat(48),
        "lead": np.tile(np.arange(1, 49), days),
    })  # fmt: skip
    rows["period_from"] = rows["issued_at"] + (rows["lead"] - 1) * fc.STEP
    rows["y"] = 150 + 50 * np.sin(rows["lead"] / 48 * 2 * np.pi) + rng.normal(0, 5, len(rows))
    day_offset = np.repeat(rng.normal(0, 15, days), 48)
    rows["a"] = rows["y"] + day_offset + rng.normal(0, 5, len(rows))
    return rows


def test_paired_bootstrap_interval_covers_a_known_gap_and_excludes_zero():
    preds = scored_preds()
    err = preds["a"] - preds["y"]
    preds["b"] = preds["y"] + err + 3 * np.sign(err)  # every error exactly 3 bigger
    diff, lo, hi = fc.paired_bootstrap(preds, "a", "b", n=500)
    assert diff == pytest.approx(-3)
    assert lo == pytest.approx(-3) and hi == pytest.approx(-3)


def test_paired_bootstrap_interval_covers_a_noisy_gap():
    preds = scored_preds(days=90)
    rng = np.random.default_rng(1)
    err = preds["a"] - preds["y"]
    extra = 3 + np.repeat(rng.normal(0, 4, 90), 48)  # 3 worse on average, varying by day
    preds["b"] = preds["y"] + err + np.sign(err) * extra.clip(min=0)
    diff, lo, hi = fc.paired_bootstrap(preds, "a", "b", n=1000)
    assert lo < diff < hi < 0 and lo < -3 * 0.6 and hi > -3 * 1.6


def test_paired_bootstrap_of_a_forecast_with_itself_is_zero():
    preds = scored_preds()
    assert fc.paired_bootstrap(preds, "a", "a", n=200) == (0.0, 0.0, 0.0)


def test_paired_bootstrap_resamples_blocks_of_days_not_half_hours():
    # One day carries the whole gap: resampling blocks of days must give a wide interval that
    # reaches 0 (draws without that day); resampling half-hours would give hi near -12.
    preds = scored_preds(days=45)
    preds["b"] = preds["a"]
    first_day = preds["issued_at"] == preds["issued_at"].min()
    preds.loc[first_day, "b"] = preds.loc[first_day, "y"] + 500
    _, lo, hi = fc.paired_bootstrap(preds, "a", "b", n=500)
    assert lo < -10 and hi == pytest.approx(0, abs=1e-9)


def test_paired_bootstrap_refuses_too_few_days():
    # 30 days = 10 blocks: too few for a percentile interval to be honest.
    with pytest.raises(ValueError, match="need 45"):
        fc.paired_bootstrap(scored_preds(days=30), "a", "a")


def test_window_regret_is_zero_when_the_forecast_is_off_by_a_constant():
    preds = scored_preds()
    preds["shifted"] = preds["y"] + 40  # wrong level, right shape: same greenest window
    regret = fc.window_regret(preds, ["shifted"], hours=3)
    assert len(regret) == preds["issued_at"].nunique()
    assert regret["shifted"].abs().max() == pytest.approx(0)


def test_window_regret_measures_how_much_dirtier_the_chosen_window_was():
    preds = scored_preds(days=1)
    preds["y"] = 100.0
    preds.loc[preds["lead"].between(11, 16), "y"] = 40.0  # truly greenest 3 h: leads 11-16
    preds["fc"] = 100.0
    preds.loc[preds["lead"].between(31, 36), "fc"] = 10.0  # forecast picks leads 31-36 (actual 100)
    assert fc.window_regret(preds, ["fc"], hours=3)["fc"].iloc[0] == pytest.approx(60)


def test_window_regret_skips_windows_with_a_gap_and_scores_columns_on_the_same_windows():
    preds = scored_preds(days=1)
    preds["y"] = 100.0
    preds.loc[preds["lead"].between(11, 16), "y"] = 40.0  # truly greenest: leads 11-16
    preds["full"] = preds["y"]  # perfect forecast
    preds["patchy"] = preds["y"]
    preds.loc[preds["lead"] == 13, "patchy"] = np.nan  # can't see the best window
    regret = fc.window_regret(preds, ["full", "patchy"], hours=3)
    # Leads 11-16 are excluded for both, so both are scored only on the remaining windows.
    assert regret["full"].iloc[0] == pytest.approx(0)
    assert regret["patchy"].iloc[0] == pytest.approx(0)
    assert len(fc.window_regret(preds[preds["lead"] < 3], ["full"], hours=3)) == 0  # no window


def test_window_regret_picks_the_same_window_as_the_dashboard():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "streamlit_app", Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py"
    )
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    preds = scored_preds(days=2, seed=3)
    preds = preds[preds["issued_at"] == preds["issued_at"].min()]
    one = preds.set_index("period_from")
    start, _ = app.greenest_window(one["a"], 3)
    end = start + 5 * fc.STEP
    expected = one.loc[start:end, "y"].mean() - one["y"].rolling(6).mean().min()
    assert fc.window_regret(preds, ["a"], hours=3)["a"].iloc[0] == pytest.approx(expected)


def test_window_regret_can_skip_partly_scored_issues():
    # A recent issue scored on its first few hours only is judged on near-term windows; with
    # full_horizon=True it is dropped, so versions with different shares of recent issues compare.
    preds = scored_preds(days=30)
    preds["b"] = preds["y"] + 5
    preds.loc[(preds["issued_at"] == preds["issued_at"].max()) & (preds["lead"] > 10), "y"] = np.nan
    assert len(fc.window_regret(preds, ["b"])) == 30
    assert len(fc.window_regret(preds, ["b"], full_horizon=True)) == 29


def test_fit_tolerates_all_missing_wind_point_columns():
    # Live state on MotherDuck until the wind-farm points are backfilled: every wind_* is NaN.
    pytest.importorskip("lightgbm")
    grid = spine(30)
    wx = hourly_weather(30)
    wx[fc.WIND_POINT_FEATURES] = np.nan
    cols = fc.FEATURES + fc.WIND_POINT_FEATURES
    model = fc.fit(fc.training_rows(grid, wx, set(), START + pd.Timedelta(days=25)), cols)
    test = fc.features(grid, wx, pd.DatetimeIndex([START + pd.Timedelta(days=27)]), set())
    assert test[fc.WIND_POINT_FEATURES].isna().all().all()
    assert np.isfinite(model.predict(test[cols])).all()
