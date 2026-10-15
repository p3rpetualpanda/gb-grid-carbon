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
