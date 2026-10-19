"""Phase 8: rolling-origin backtest of the 48h forecast -> docs/backtest.md + chart.

Also writes the headline numbers to docs/backtest_results.json (read by the dashboard); then run
`python scripts/update_backtest_docs.py` to copy them into README.md and docs/showcase.md.

Run after `dbt build` (local warehouse):  python analysis/backtest.py   (~1-3 min)
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from gridcarbon import forecast as fc  # noqa: E402
from gridcarbon.config import Settings  # noqa: E402
from gridcarbon.load import connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")  # the report prints "gCO₂"; Windows' cp1252 can't
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#7c5cd6",
)
INK, INK2, MUTED, GRID, AXIS, SURFACE = (
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
    "#fcfcfb",
)

con = connect(Settings(db_target="local"))
grid, wx = fc.load_history(con)
hols = fc.holidays()
neso = fc.load_neso_forecasts(con)
# The live model (v3: + NESO embedded wind/solar), the previous one (v2: no NESO inputs) and one
# that also adds NESO's transmission wind forecast, all trained and scored on the same folds.
variants = {
    "model": fc.FEATURES,
    "model_v2": fc.BASE_FEATURES,
    "model_neso_wind": fc.BASE_FEATURES + fc.NESO_FEATURES,
}
preds = fc.backtest(grid, wx, hols, months=12, neso=neso, variants=variants)
methods = {"model": "Our model (LightGBM + NESO embedded wind/solar)",
           "model_v2": "Previous model (no NESO inputs)",
           "model_neso_wind": "+ NESO transmission wind",
           "neso": "NESO published forecast*",
           "last_actual": "Last known actual",
           "lag_day": "Same slot, last known day", "lag_week": "Same slot, last week"}  # fmt: skip
table = fc.mae_by_lead(preds, list(methods))

# Chart: MAE by lead band, one line per method (legend + direct labels).
bands = [b for b in table.index if b != "all"]
fig, ax = plt.subplots(figsize=(8, 5.2), dpi=150)
fig.patch.set_facecolor(SURFACE)
ax.set_facecolor(SURFACE)
for (col, label), colour in zip(
    methods.items(), (BLUE, MUTED, VIOLET, ORANGE, AQUA, YELLOW, MAGENTA), strict=True
):
    ax.plot(bands, table.loc[bands, col], color=colour, linewidth=2, marker="o", markersize=5,
            label=label)  # fmt: skip
    ax.annotate(f"{table.loc[bands[-1], col]:.0f}", (len(bands) - 1, table.loc[bands[-1], col]),
                xytext=(8, 0), textcoords="offset points", va="center", color=INK2)  # fmt: skip
ax.set_title("Forecast error by lead time (12-month rolling backtest)", loc="left", color=INK)
ax.set_ylabel("MAE, gCO₂/kWh (lower is better)", color=INK2)
ax.set_xlabel("How far ahead", color=INK2)
ax.tick_params(colors=MUTED, length=0)
ax.grid(axis="y", color=GRID, linewidth=0.8)
ax.set_axisbelow(True)
for side in ("top", "right", "left"):
    ax.spines[side].set_visible(False)
ax.spines["bottom"].set_color(AXIS)
ax.legend(frameon=False, labelcolor=INK2, loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2)
fig.tight_layout()
(ROOT / "docs" / "img").mkdir(parents=True, exist_ok=True)
fig.savefig(ROOT / "docs" / "img" / "backtest_mae_by_lead.png", facecolor=SURFACE)

# Feature importance from a model trained on everything (gain = total loss reduction).
model = fc.fit(fc.training_rows(grid, wx, hols, preds["issued_at"].max(), neso))
gain = pd.Series(model.booster_.feature_importance("gain"), index=fc.FEATURES)
gain = (gain / gain.sum()).sort_values(ascending=False)


def md(df: pd.DataFrame) -> str:
    """Markdown table without the optional `tabulate` dependency."""
    rows = [[str(i), *(f"{v:.3g}" if isinstance(v, float) else str(v) for v in r)]
            for i, r in zip(df.index, df.to_numpy(), strict=True)]  # fmt: skip
    head = ["", *map(str, df.columns)]
    return "\n".join("| " + " | ".join(r) + " |" for r in [head, ["---"] * len(head), *rows])


overall = table.loc["all"]
best_baseline = overall[["last_actual", "lag_day", "lag_week"]].min()
md_table = md(table.rename(columns=methods).round(1))

# Bias (forecast minus actual), window regret and intervals, all on the rows mae_by_lead scores.
complete = preds.dropna(subset=list(methods))
signed = complete[list(methods)].sub(complete["y"], axis=0)
lead_band = pd.cut(complete["lead"] * 0.5, [0, 6, 12, 24, 48],
                   labels=["0-6h", "6-12h", "12-24h", "24-48h"])  # fmt: skip
bias = signed.groupby(lead_band, observed=True).mean()
bias.loc["all"] = signed.mean()
month = complete["period_from"].dt.tz_convert("Europe/London").dt.strftime("%Y-%m")
bias_month = signed[["model", "model_v2", "neso"]].groupby(month).mean()
regret = fc.window_regret(complete, list(methods), hours=3)
regret_table = pd.DataFrame(
    {"mean regret": regret.mean(), "share within 5 of best": (regret <= 5).mean()}
).rename(index=methods)
intervals = pd.DataFrame(
    [fc.paired_bootstrap(complete, v, "model") for v in ("model_v2", "model_neso_wind")],
    index=[methods["model_v2"], methods["model_neso_wind"]],
    columns=["MAE minus live model", "95% low", "95% high"],
)
report = f"""# Forecast backtest

_Generated by `analysis/backtest.py`. {preds["issued_at"].nunique():,} forecasts issued every 6 h
over the last 12 months, each predicting the next 48 h (96 half-hours): {len(preds):,} predictions._

**Method.** Direct multi-horizon LightGBM: one model, lead time as a feature, pulled towards the
last known actual at short leads (weight exp(-(lead-1)/6), tuned on the first 6 test months only).
Training issues every 6.5 h, so every half-hour of the day is seen as an issue time. Rolling origin: for
each test month the model is retrained only on rows whose *targets* fall before the month starts.
Features use only data available at issue time: actuals end 1 h before issue (publication lag),
"same slot" lags reach back as many whole days as needed to land on known data, and weather is
Open-Meteo's *historical forecast*, never observed weather. These rules are enforced by
`tests/test_forecast.py`.

**Result.** Over all leads the model's MAE is **{overall["model"]:.1f} gCO₂/kWh**, vs
**{best_baseline:.1f}** for the best naïve baseline (**{1 - overall["model"] / best_baseline:.0%}
better**) and **{overall["neso"]:.1f}** for NESO's published forecast. The model uses NESO's own
embedded wind/solar forecasts as inputs (the vintage issued before each forecast, never a later
one); without them (the previous model) it scores **{overall["model_v2"]:.1f}**. Also adding
NESO's transmission wind forecast gives **{overall["model_neso_wind"]:.1f}**, but its archive only
covers the day each forecast is published, so most 48 h targets lack it and it is left out.

![MAE by lead](img/backtest_mae_by_lead.png)

{md_table}

\\* **Read the NESO column with care.** The API only keeps NESO's *final* published forecast for
each half-hour, not when it was made, so it is likely a short-lead forecast. That makes it a
flattering benchmark at long leads. The fair head-to-head is the live track record
(`marts.fct_model_vs_official`), which compares both forecasts issued in the same pipeline run.

**Caveat.** Open-Meteo's historical forecasts are stitched from the first hours of each model run,
so training weather is closer to a short-range forecast than a true 48 h one. Expect live errors
at long leads to be somewhat higher than this backtest.

**Bias** (mean of forecast minus actual, gCO₂/kWh; + = too high), by lead and by month:

{md(bias.rename(columns=methods).round(1))}

{md(bias_month.rename(columns=methods).round(1))}

**Greenest 3 h window.** For each forecast, pick the cleanest 3 h block it predicts and compare
the actual intensity in that block with the truly cleanest block. Regret is the gap in gCO₂/kWh
(0 = picked the best block); a forecast that is off by a constant has zero regret.

{md(regret_table.round(3))}

**Is a variant really different from the live model?** MAE difference on the same rows, with a
95% interval from resampling 3-day blocks (errors within a day, and weather spells, move
together). A variant is better
only if the whole interval is below zero.

{md(intervals.round(2))}

**What drives the model** (share of total gain):

{md(gain.round(3).to_frame("gain_share"))}
"""
(ROOT / "docs" / "backtest.md").write_text(report, encoding="utf-8")
results = {
    "run_date": datetime.now(UTC).date().isoformat(),
    "forecasts": int(preds["issued_at"].nunique()),
    "predictions": len(preds),
    "model_mae": round(float(overall["model"]), 1),
    "best_baseline_mae": round(float(best_baseline), 1),
    "improvement": round(float(1 - overall["model"] / best_baseline), 3),
    "neso_mae": round(float(overall["neso"]), 1),
}
(ROOT / "docs" / "backtest_results.json").write_text(
    json.dumps(results, indent=2) + "\n", encoding="utf-8"
)
print(report)
