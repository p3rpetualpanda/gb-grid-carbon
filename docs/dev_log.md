# Development log

Short notes on decisions and surprises; source material for the README and for interviews.

## 2026-10-12: Phases 1–3: scaffold, API exploration, extraction layer

**Probed every endpoint before writing code.** Findings that changed the design:

1. **Per-endpoint range limits differ**: intensity 31 days, generation 60, regional 14. The plan
   assumed one limit for all; the extractors now chunk per endpoint.
2. **"Greater than 31 days" also rejects exactly 31 days.** The first full backfill died with
   HTTP 400 on its first window (`2017-09-11T23:00Z/2017-10-12T23:00Z`). Boundary tests showed
   intensity/regional reject a span of exactly the limit but accept the limit minus 30 minutes;
   generation accepts exactly 60. Explanation: the API also returns the half-hour *ending* at
   `from`, so the effective span is limit + 30 min. Fix: whole-day windows strictly under the
   limit (30 / 60 / 13 days) plus a regression test that asserts every request span < limit.
   Unit tests with mocked HTTP could not have caught this; only running against the real API did.
3. **Overlapping windows:** because of (2), consecutive requests share one boundary period.
   Extractors keep `period_from ∈ [from, to)` and loads upsert by key, so nothing double counts.
   A test fakes the API's exact behaviour across two chunks and asserts 48 unique periods/day.
4. **Region IDs 15–18 are aggregates** (England, Scotland, Wales, GB). Summing all 18 would
   double count. Documented; dbt models must filter.
5. **DuckDB needs `pytz`** to return TIMESTAMPTZ values to Python; added as a dependency
   (tests caught it immediately).

6. **Open-Meteo rate-limits by weighted calls** (each location and each ~2 weeks of data count
   separately), so yearly 7-point backfill windows tripped the per-minute quota (HTTP 429). The
   old backoff gave up after ~15 s; it now backs off for ~62 s, enough to outlast a per-minute
   window. Because backfills are resumable (`meta.load_log`), re-running just continued from the
   failed window.

7. **Silent truncation at year end (found by a data-quality check, not by an error).** After the
   backfill, a gap query against a half-hour time spine showed generation mix missing 10.4% of
   periods, and *every* January 2021–2024 completely (1,488 = 31 × 48 half-hours each). Every
   missing month sat in a 60-day window crossing New Year. Direct probes confirmed the generation
   and regional endpoints return HTTP 200 but stop at 31 Dec 23:00 when a request spans a year
   boundary; national intensity does not. Fix: `chunk_range(..., split_years=True)` for those two
   endpoints + regression test, then `backfill -d generation --no-resume` (safe: upserts).
   Missing generation periods: **10.41% → 0.29%**. Lesson: "no errors" ≠ "complete data", which
   is why Phase 5 adds a time-spine gap test in dbt.

Coverage after backfill (missing half-hours vs a complete time spine): national intensity
179 / 159,246 (0.11%; worst 2023-10, 2021-12); generation 422 / 147,677 (0.29%; worst 2018-07/08,
2023-10). 626 intensity periods have no `actual`. Fuel shares sum to 99.7–100.3%.

**Idempotent loads = upsert by natural key, not "delete the window then insert".** A delete-window
approach would wipe history if the API returned a partial or empty response during an outage.
Upsert-by-key only replaces rows the batch actually contains. Verified live: running
`gridcarbon incremental` twice leaves 145 rows / 145 distinct periods.

**Forecast snapshots are append-only by `issued_at`.** Past forecasts can't be re-downloaded,
so the pipeline must capture them as it runs. This is what lets us score NESO's forecast (and
ours) against actuals later.

## 2026-10-12: Phase 4: MotherDuck

- Same code, one env var: `DB_TARGET=motherduck` makes `connect()` use hosted DuckDB; no SQL
  changes were needed.
- **Connecting to a database that doesn't exist (`md:gridcarbon`) hangs instead of erroring.**
  `connect()` now connects to `md:`, runs `CREATE DATABASE IF NOT EXISTS`, then `USE`.
- History was loaded by re-running the resumable backfill against MotherDuck (zero new code)
  rather than writing a copy job; then row counts were compared with the local warehouse.
- The token lives only in the gitignored `.env` (locally) and, from Phase 6, in GitHub Secrets.

## 2026-10-12: Phases 5–6: dbt + scheduled pipeline

- **Layers:** `staging` (rename + units, views) → `intermediate` (time spine, UK calendar, wide
  mix, GB weather) → `marts` (tables). UTC everywhere; `int_calendar` is the only place that
  converts to Europe/London (verified: clock-change days have 46 and 50 half-hours).
- **Kept small on purpose:** no dbt packages (custom checks are four singular SQL tests),
  full-refresh tables instead of incremental (~2M rows rebuild in ~7 s on MotherDuck), and only
  the marts the analysis/dashboard need. A heatmap aggregate can be queried on the fly.
- **The physical-range test failed on the first run:** 16 NESO forecasts in 2018–19 are impossible
  (up to 13,579 gCO₂/kWh vs actuals ~150–300). Raw keeps them; staging nulls them and sets
  `is_forecast_invalid`. Another defect found by a test, not by eye.
- **Bank holidays:** gov.uk's feed starts in 2019; Dec 2017 – Dec 2018 were added by hand.
- **CI** runs `dbt build` on an empty warehouse, so every model and test compiles and runs on each
  PR without touching production.
- **Pipeline** (`.github/workflows/pipeline.yml`, every 3 h): incremental load → source freshness
  (warn 6 h / error 24 h) → `dbt build` → dbt docs published to GitHub Pages.

## 2026-10-12: Phases 7-9: findings, forecast, dashboard

- **Findings as a script, not a notebook:** `analysis/findings.py` computes every number and writes
  `docs/findings.md` + charts, so the write-up can never drift from the data.
- **Forecast:** direct multi-horizon LightGBM, retrained each pipeline run (seconds; no model
  artifact to store). Backtest MAE 26.8 vs 44.3 naive.
- **Live sanity check found a real problem:** first live forecast ~170 at 13:30 vs last actual 88
  (truth 86). Fixed a train/serve skew (6 h issue grid -> 6.5 h, coprime with 48 half-hours) and
  added a persistence blend for short leads, tuned on the first half of the backtest and checked
  on the second (0-2 h MAE 19.1 -> 17.0). Persistence is now also a reported baseline.
- **Dashboard:** one Streamlit file, marts only, MotherDuck via secret or local file fallback.
  Found a pandas name clash (`df.rolling` column vs method) only by rendering it.

## 2026-10-16: Pipeline every 2 h

- The schedule moved from every 3 h to every 2 h (`17 */2 * * *`); user-facing text says "every
  few hours" because GitHub's start times drift.

## 2026-10-18: NESO forecasts as inputs, dashboard, schedule

- **NESO wind and solar forecasts loaded, every vintage.** Day-ahead transmission wind and 0-14 day
  embedded wind/solar from the NESO Data Portal since 2022 (~6M embedded rows), keyed on issue
  time so a backtest can use only the vintage published before each forecast (plus a 30 min
  allowance). Checked against the live API: only the yearly archives carry the issue timestamp
  (resources are picked by their fields, not names); embedded `TIME_GMT` is the *end* of the
  half-hour; the wind archive has empty weeks (Jun-Sep 2023, Oct-Nov 2025, Jan and May 2026).
- **Backtest: MAE 26.8 -> 21.0** with those forecasts as inputs, better at every lead. Kept as a
  candidate, not the live model: the day-ahead wind input is missing for most rows, because each
  vintage only covers the day it is published.
- **14-day wind forecast, snapshotted.** NESO's 14-day transmission wind forecast covers the full
  48 h but keeps only its latest vintage, so every run now appends it to
  `raw.neso_wind_14d_snapshots` (like the intensity forecast). Its timestamps are UTC: the
  25 Oct clock-change day has 50 settlement periods. Over time this gives the model a full-horizon
  wind input.
- **Dashboard additions:** a ± typical-error band on the forecast (live track-record MAE by lead,
  for whichever forecast is shown), a wind/solar outlook (`fct_wind_solar_outlook`: newest vintage
  per half-hour), the live generation mix, and a plain-English forecast accuracy tab.
- **A new dbt table crashed the live app.** The app code deployed on merge, but MotherDuck only
  built the table on the next pipeline run, so the Now tab raised `CatalogException`. Fix: queries
  on new tables return no rows while the table is missing (`rows_or_empty`), and the pipeline was
  run straight away.
- **GitHub skipped half the scheduled runs.** At every 2 h, only 12 of 26 scheduled runs started
  over 16-18 Oct (median gap 4.4 h, max 6.5 h). The schedule now asks hourly, and cron-job.org
  calls the `workflow_dispatch` API every 2 h with a fine-grained token (Actions read/write on
  this repo only; expires 17 Oct 2027). Overlapping runs are blocked by the concurrency group and
  loads are idempotent, so duplicate triggers are harmless.
