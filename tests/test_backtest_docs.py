import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "update_backtest_docs",
    Path(__file__).resolve().parents[1] / "scripts" / "update_backtest_docs.py",
)
docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(docs)


def test_render_replaces_only_marked_numbers():
    vals = docs.values({"run_date": "2026-01-02", "model_mae": 25.04, "best_baseline_mae": 40,
                        "neso_mae": 9.94, "improvement": 0.374,
                        "previous_model_mae": 27.0, "v3_model_mae": 20.9})  # fmt: skip
    text = "M <!-- bt:model_mae -->26.8<!-- /bt --> is <!-- bt:improvement -->40%<!-- /bt --> 9"
    assert docs.render(text, vals) == (
        "M <!-- bt:model_mae -->25.0<!-- /bt --> is <!-- bt:improvement -->37%<!-- /bt --> 9"
    )


def test_readme_and_showcase_match_results_file():
    # Fails when backtest_results.json changed but update_backtest_docs.py wasn't re-run.
    assert docs.main(check=True) == 0
