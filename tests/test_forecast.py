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
    return pd.DataFrame(
        {"wind_speed": rng.uniform(0, 60, len(idx)), "solar_radiation": 0.0, "temperature": 10.0},
        index=idx,
    )


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
