import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "streamlit_app", Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py"
)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)  # safe: the UI only runs under __main__

IDX = pd.date_range("2024-01-01", periods=8, freq="30min", tz="UTC")
SERIES = pd.Series([100, 90, 50, 40, 45, 200, 10, 300], index=IDX, dtype=float)


def test_greenest_window_finds_cleanest_contiguous_block():
    # 1.5 h = 3 half-hours: [50, 40, 45] beats the single 10 surrounded by 200 and 300.
    assert app.greenest_window(SERIES, 1.5) == (IDX[2], 45.0)
    assert app.greenest_window(SERIES, 0.5) == (IDX[6], 10.0)


def test_best_forecast_uses_neso_for_first_6h_when_present():
    issue = pd.DataFrame({
        "period_from": IDX[:4], "lead": [1, 12, 12, 13],
        "model_gco2_kwh": [1.0, 2.0, 3.0, 4.0], "neso_gco2_kwh": [10, 20, None, 40],
    })  # fmt: skip
    out = app.best_forecast(issue)
    assert out.tolist() == [10, 20, 3, 4] and out.index.equals(IDX[:4])


def test_greenest_window_rejects_too_short_forecast():
    with pytest.raises(ValueError):
        app.greenest_window(SERIES, 8)


def test_backtest_caption_reads_results_file(tmp_path):
    path = tmp_path / "results.json"
    path.write_text('{"run_date": "2026-01-02", "model_mae": 25.04, "best_baseline_mae": 40}')
    caption = app.backtest_caption(path)
    assert "run 2026-01-02" in caption and "25.0 MAE vs 40.0" in caption
    assert app.backtest_caption(tmp_path / "missing.json").startswith("12-month backtest: [")


def test_typical_error_follows_the_forecast_shown_and_its_lead_band():
    issue = pd.DataFrame({
        "period_from": IDX[:4], "lead": [1, 12, 13, 60],
        "model_gco2_kwh": [1.0, 2.0, 3.0, 4.0], "neso_gco2_kwh": [10, None, 30, 40],
    })  # fmt: skip
    scored = pd.DataFrame({
        "band": ["0-6 h", "6-12 h", "24-48 h", "All"],
        "our_mae": [5.0, 6.0, 8.0, 7.0], "neso_mae": [2.0, 3.0, 4.0, 3.0],
    })  # fmt: skip
    out = app.typical_error(issue, scored)
    # NESO's error where NESO is shown (lead <= 12 with a value), ours otherwise.
    assert out.tolist() == [2.0, 5.0, 6.0, 8.0] and out.index.equals(IDX[:4])
    assert app.typical_error(issue, scored.iloc[:0]).isna().all()  # no track record yet


def test_past_forecast_takes_each_half_hour_from_the_lowest_mae_lead_band():
    issues = pd.DataFrame({
        "period_from": [IDX[0], IDX[0], IDX[0], IDX[1], IDX[1], IDX[2]],
        "lead": [2, 14, 20, 3, 30, 5],
        "model_gco2_kwh": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    })  # fmt: skip
    scored = pd.DataFrame({
        "band": ["0-6 h", "6-12 h", "12-24 h", "All"], "our_mae": [30.0, 10.0, 20.0, 15.0],
    })  # fmt: skip
    out = app.past_forecast(issues, scored)
    # 6-12 h scores best, newest issue (shortest lead) within it; else the next-best band.
    assert out.tolist() == [2.0, 5.0, 6.0] and out.index.equals(IDX[:3])
    # No track record yet: the newest issue for each half-hour.
    assert app.past_forecast(issues, scored.iloc[:0]).tolist() == [1.0, 4.0, 6.0]


def test_typical_error_uses_the_current_versions_mae_once_it_has_enough_rows():
    issue = pd.DataFrame({
        "period_from": IDX[:3], "lead": [13, 30, 60],
        "model_gco2_kwh": [1.0, 2.0, 3.0], "neso_gco2_kwh": [None, None, None],
    })  # fmt: skip
    scored = pd.DataFrame({  # every model version
        "band": ["6-12 h", "12-24 h", "24-48 h", "All"],
        "our_mae": [6.0, 7.0, 8.0, 7.0], "neso_mae": [3.0, 4.0, 5.0, 4.0],
    })  # fmt: skip
    current = pd.DataFrame({  # the version that issued the forecast shown
        "band": ["6-12 h", "12-24 h"], "n": [150, 40], "our_mae": [2.0, 1.0],
    })  # fmt: skip
    out = app.typical_error(issue, scored, current, min_n=100)
    # 6-12 h has enough rows for its own MAE; 12-24 h (40 rows) and 24-48 h (none) use all versions.
    assert out.tolist() == [2.0, 7.0, 8.0]
    assert app.mixes_versions(issue, current, min_n=100)
    assert not app.mixes_versions(issue.iloc[:1], current, min_n=100)
    assert app.typical_error(issue, scored, current.iloc[:0]).tolist() == [6.0, 7.0, 8.0]


def test_rows_or_empty_survives_a_table_dbt_has_not_built_yet():
    import duckdb

    con = duckdb.connect()
    assert app.rows_or_empty(lambda sql: con.sql(sql).df(), "select * from nope").empty
    with pytest.raises(duckdb.ParserException):  # other errors still surface
        app.rows_or_empty(lambda sql: con.sql(sql).df(), "selec 1")


def test_day_windows_wrap_past_midnight():
    # A typical day by half-hour slot: cleanest at 23:00-00:30 (slots 46, 47, 0), dirtiest 18:00.
    day = pd.Series(100.0, index=range(48))
    day[[46, 47, 0]] = 10.0
    day[36] = 400.0
    means = app.day_windows(day, 1.5)
    assert len(means) == 48  # one window per start slot, wrapping into the next day
    assert means.idxmin() == 46 and means.min() == 10.0
    assert means.idxmax() in (34, 35, 36)


def test_slot_label():
    assert app.slot_label(0) == "00:00" and app.slot_label(47) == "23:30"
    assert app.slot_label(50) == "01:00"  # a window ending after midnight


def test_upcoming_drops_half_hours_already_past():
    forecast = pd.Series(1.0, index=pd.date_range(IDX[0], periods=40, freq="30min"))
    now = IDX[0] + pd.Timedelta(minutes=95)  # inside the 4th half-hour
    out, ahead = app.upcoming(forecast, now, min_hours=8)
    assert out.index[0] == IDX[3] and len(out) == 37 and ahead == 18.5
    # Too little forecast left to plan with: keep the whole (stale) issue rather than crash,
    # but still report how little of it is ahead.
    out, ahead = app.upcoming(forecast, now + pd.Timedelta(hours=15), min_hours=8)
    assert len(out) == 40 and ahead == 3.5


def test_band_sql_matches_the_python_bands():
    import duckdb

    leads = pd.DataFrame({"lead": [1, 12, 13, 24, 25, 48, 49, 96]})
    sql = duckdb.sql(f"select {app.BAND_SQL} as band from leads").df()["band"]
    assert sql.tolist() == app._bands(leads).tolist()


def test_typical_day_weights_weekdays_and_weekends_by_count():
    def rows(month, slots, weekend, g, n):
        return pd.DataFrame({"month": month, "halfhour_slot": slots, "is_weekend": weekend,
                             "g": g, "n": n})  # fmt: skip

    typical = pd.concat([
        rows(1, range(48), False, 100.0, 5), rows(1, range(48), True, 30.0, 2),
        rows(2, range(47), False, 1.0, 1),
    ])  # fmt: skip
    out = app.typical_day(typical)
    assert out.index.tolist() == [1]  # month 2 lacks a slot
    assert (out.loc[1] == 80.0).all()  # (5 * 100 + 2 * 30) / 7, not the plain mean 65


def test_hhmm_survives_missing_timestamps():
    assert app.hhmm(pd.NaT) == "–"
    assert app.hhmm(pd.Timestamp("2024-07-01 12:00", tz="UTC")) == "Mon 13:00"


def test_motherduck_database_follows_the_pipeline_setting(monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_DATABASE", raising=False)
    assert app.motherduck_url() == "md:gridcarbon"
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "gridcarbon_dev")
    assert app.motherduck_url() == "md:gridcarbon_dev"


def test_example_is_the_latest_fully_scored_issue():
    import duckdb

    con = duckdb.connect()
    con.execute("create schema marts")
    con.execute("""
        create table marts.fct_model_vs_official as
        select * from (values
            ('2024-01-01'::timestamp, 1, 100), ('2024-01-01', 2, 100),  -- complete
            ('2024-01-02', 1, 100), ('2024-01-02', 2, null),          -- newer, part scored
            ('2024-01-03', 1, null), ('2024-01-03', 2, null)
        ) t(issued_at, period_from, actual_gco2_kwh)
    """)
    example = con.sql(app.EXAMPLE_SQL.replace(
        "issued_at, period_from, model_gco2_kwh, neso_gco2_kwh, actual_gco2_kwh", "*"
    )).df()  # fmt: skip
    assert set(example["issued_at"]) == {pd.Timestamp("2024-01-01")}
