"""Experiment: does a neural network beat the live LightGBM model on the same backtest?

Same 12 monthly folds, rows, features, leakage rules and persistence blend as
analysis/backtest.py; only the regressor differs. Writes data/nn_backtest.md (git-ignored)
until the result is worth publishing.

Needs CUDA PyTorch in the project venv (not a project dependency: the pipeline doesn't use it):
  .venv\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cu128
Run after `dbt build` (local warehouse):  python analysis/nn_backtest.py   (~1-3 min on a GPU)
"""

from __future__ import annotations

import sys
import time
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn

from gridcarbon import forecast as fc
from gridcarbon.config import Settings
from gridcarbon.load import connect

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
MONTHS = 12
EPOCHS, BATCH, LR = 20, 4096, 1e-3  # fixed up front, never tuned on the test folds
SEEDS = 5  # the NN forecast is the mean of this many runs; single seeds are scored too
SEED_COLS = [f"nn_seed{s}" for s in range(SEEDS)]
CYCLIC = {"halfhour_slot": 48, "iso_weekday": 7, "month": 12}
NUMERIC = [c for c in fc.FEATURES if c not in CYCLIC]
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


class MLP:
    """Standardised inputs, NaN -> 0 plus a missing flag (LightGBM handles NaN natively; an MLP
    can't), calendar as sin/cos so 23:30 sits next to 00:00. L1 loss because MAE is the score."""

    def fit(self, rows: pd.DataFrame, seed: int) -> MLP:
        rows = rows.dropna(subset=["y"])
        self.mean = rows[NUMERIC].mean()
        self.std = rows[NUMERIC].std().replace(0, 1)
        self.y_mean, self.y_std = rows["y"].mean(), rows["y"].std()
        x = self._x(rows)
        y = torch.tensor(((rows["y"] - self.y_mean) / self.y_std).to_numpy(), dtype=torch.float32)
        y = y.to(DEVICE)
        torch.manual_seed(seed)
        self.net = nn.Sequential(
            nn.Linear(x.shape[1], 256), nn.SiLU(), nn.Dropout(0.1),
            nn.Linear(256, 256), nn.SiLU(), nn.Dropout(0.1),
            nn.Linear(256, 256), nn.SiLU(),
            nn.Linear(256, 1),
        ).to(DEVICE)  # fmt: skip
        opt = torch.optim.AdamW(self.net.parameters(), lr=LR, weight_decay=1e-4)
        steps = EPOCHS * -(-len(x) // BATCH)
        sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=LR, total_steps=steps)
        self.net.train()
        for _ in range(EPOCHS):
            for idx in torch.randperm(len(x), device=DEVICE).split(BATCH):
                loss = (self.net(x[idx]).squeeze(1) - y[idx]).abs().mean()
                opt.zero_grad()
                loss.backward()
                opt.step()
                sched.step()
        self.final_loss = loss.item() * self.y_std  # last batch, gCO2/kWh: a sanity check only
        return self

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        self.net.eval()
        with torch.no_grad():
            out = self.net(self._x(rows)).squeeze(1).cpu().numpy()
        return out * self.y_std + self.y_mean

    def _x(self, rows: pd.DataFrame) -> torch.Tensor:
        z = (rows[NUMERIC] - self.mean) / self.std
        angle = {c: 2 * np.pi * rows[c].to_numpy() / n for c, n in CYCLIC.items()}
        x = np.column_stack(
            [
                z.fillna(0).to_numpy(),
                z.isna().to_numpy(),
                *(f(a) for a in angle.values() for f in (np.sin, np.cos)),
            ]
        ).astype(np.float32)
        return torch.from_numpy(x).to(DEVICE)


con = connect(Settings(db_target="local"))
grid, wx = fc.load_history(con)
hols = fc.holidays()
neso = fc.load_neso_forecasts(con)
print(f"device: {torch.cuda.get_device_name(0) if DEVICE == 'cuda' else 'cpu'}")

# shortcut: copies fc.backtest's fold loop (6 lines) to add a non-LightGBM regressor; give
# fc.backtest a `fitters` argument if the NN ever becomes a live candidate.
last = grid["y"].dropna().index.max()
folds = pd.date_range(end=last.floor("D"), periods=MONTHS + 1, freq="MS")
out = []
for fold_start, fold_end in pairwise(folds):
    t0 = time.perf_counter()
    train = fc.training_rows(grid, wx, hols, fold_start, neso)
    test = fc.features(grid, wx, fc.issue_times(fold_start, fold_end - fc.STEP), hols, neso)
    # Leakage guard, as in tests/test_forecast.py: every training target precedes the fold.
    assert train["period_from"].max() < fold_start
    test["model"] = fc.blend(test, fc.fit(train, fc.FEATURES).predict(test[fc.FEATURES]))
    for seed, col in enumerate(SEED_COLS):
        mlp = MLP().fit(train, seed)
        test[col] = fc.blend(test, mlp.predict(test))
    test["nn"] = test[SEED_COLS].mean(axis=1)  # blend is linear, so this is the blended mean
    out.append(test)
    print(
        f"{fold_start:%Y-%m}: {len(train):,} train rows, last-batch L1 {mlp.final_loss:.1f}, "
        f"{time.perf_counter() - t0:.0f} s"
    )
preds = pd.concat(out, ignore_index=True).dropna(subset=["y"])
preds["mean_of_both"] = (preds["model"] + preds["nn"]) / 2

methods = {
    "model": "LightGBM (live v3)",
    "nn": f"Neural net (mean of {SEEDS} seeds)",
    "mean_of_both": "Mean of both",
    "last_actual": "Last known actual",
    "lag_day": "Same slot, last known day",
}
table = fc.mae_by_lead(preds, list(methods)).rename(columns=methods).round(1)
complete = preds.dropna(subset=list(methods))
regret = fc.window_regret(complete, list(methods), hours=3).mean().rename(methods).round(2)
intervals = pd.DataFrame(
    [fc.paired_bootstrap(complete, v, "model") for v in ("nn", "mean_of_both", *SEED_COLS)],
    index=[methods["nn"], methods["mean_of_both"], *(f"Single seed {s}" for s in range(SEEDS))],
    columns=["MAE minus LightGBM", "95% low", "95% high"],
).round(2)

report = f"""# Neural net vs LightGBM backtest

_Generated by `analysis/nn_backtest.py` ({DEVICE}). {preds["issued_at"].nunique():,} forecasts
over the last {MONTHS} months, {len(complete):,} scored half-hours. Both models see the same rows,
features ({len(fc.FEATURES)}), folds and persistence blend._

MLP: 3 x 256 hidden, SiLU, L1 loss, AdamW, {EPOCHS} epochs, one-cycle LR {LR:g}, batch {BATCH};
fixed before the run, not tuned on the test folds. Untuned, so a small loss doesn't rule out an
MLP. The NN forecast is the mean of {SEEDS} seeds; each single seed is also scored below, to show
seed-to-seed spread. The persistence blend was tuned for LightGBM. Scored rows need every method below, not NESO's or last week's value as in
docs/backtest.md, so the LightGBM MAE here differs slightly from the published one.

**MAE by lead (gCO₂/kWh):**

{table.to_markdown()}

**Mean greenest-3h-window regret (gCO₂/kWh):**

{regret.to_frame("mean regret").to_markdown()}

**Better than LightGBM?** MAE difference on the same rows, 95% interval from 3-day blocks.
Better only if the whole interval is below zero.

{intervals.to_markdown()}
"""
(ROOT / "data" / "nn_backtest.md").write_text(report, encoding="utf-8")
print(report)
