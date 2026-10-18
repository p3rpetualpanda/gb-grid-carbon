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
