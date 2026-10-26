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

## 2026-10-18: NESO-input model goes live (v3)

- **Live model is now `lgbm-direct-v3`**: the v2 features plus NESO's embedded wind and solar
  forecasts. 12-month backtest MAE 26.8 -> 20.9, better at every lead. The embedded forecast is
  refreshed every run and reaches about 3 days ahead, so it covers the full 48 h live.
- **Transmission wind left out.** With it the backtest is also 20.9 (slightly worse at 0-6 h), and
  its day-ahead archive covers only the day each forecast is published, so live rows would mostly
  lack it. It can come back from `raw.neso_wind_14d_snapshots` once that has some history.
- **Fallback if NESO stalls.** A stale vintage leaves later targets uncovered, which training
  almost never saw. If any live target lacks an embedded value, the run trains on the v2 features
  instead and labels the forecast `lgbm-direct-v2`, so the track record shows which model ran.
- **Known gap:** embedded forecasts start in 2019, so 2017-2018 training rows have them missing.
  LightGBM handles NaN, and the backtest scores the result, but the model could learn "missing
  means an old year". Restricting training to 2019+ is worth testing.

## 2026-10-19: trend fixes tested, v3 kept; fallback label

- **No trend fix ships.** Three variants were scored against v3 on the same 12 backtest folds,
  with day-blocked bootstrap intervals. Training on 2021+ only changed nothing: training
  already starts in 2022, when Open-Meteo's historical forecasts begin, so the 2017-2021 years
  were never in it. Predicting the gap to `lag_day` was worse (MAE +0.14, interval -0.06 to
  +0.35). Recency weights with a 2-year half-life cut MAE by 0.29 (interval -0.49 to -0.09) and
  bias to 0.0-0.7, but widened the monthly bias swing (spread 26.3 -> 29.7) and made the
  greenest-window choice slightly worse (regret 6.63 -> 6.78). That fails the "monthly bias
  narrows" gate, so v3 stays. The remaining error is month-to-month swings (-17 to +10), which is
  what error feedback targets.
- **Fallback label.** The NESO fallback was labelled `lgbm-direct-v2`, the same as the old
  model, so the 6 pre-v3 forecasts on 18 Oct looked like fallback runs. None were: every run
  since v3 merged used v3. The fallback is now `lgbm-direct-v3-fallback`.

## 2026-10-19: review fixes and a retry for non-JSON replies

- **Retry a 200 that isn't JSON.** Twice in a day (18 Oct 17:17, 19 Oct 01:21) Open-Meteo answered
  200 with a body that wasn't JSON, and the whole pipeline run failed. `ApiClient` now treats
  that as transient and retries it like a 5xx; if it never parses, the run still fails loudly.
- **Bootstrap minimum 20 -> 45 days.** 20 days is about 7 three-day blocks, too few for an honest
  percentile interval. `live_bias.py` says "no interval yet" until 45 days are scored.
- **Regret on fully scored issues only in `live_bias.py`** (`window_regret(full_horizon=True)`).
  Recent issues have actuals for their first hours only, so they were judged on near-term
  windows and made versions with many recent issues look different for no real reason.
- **NESO coverage logged, fallback kept strict.** 99.9% of v3's training rows have NESO embedded
  inputs (checked on the local warehouse), so a live row without them is something the model has
  barely seen; falling back for the whole issue is the right call. `predict_live` now logs the
  coverage share each run so a thinning history would show up.

## 2026-10-19: "When is it greenest?" tab reworked

- **From one heatmap to an answer.** The tab now asks for a month and a task length, then states
  the greenest and dirtiest window of that length (UK time, last 12 months) and the share of
  emissions saved by moving from one to the other. A typical-day chart splits weekdays from
  weekends (weekends are much cleaner) and shades the greenest window, and a table lists both
  windows for every month. The heatmap keeps the all-months view, now with month names, times
  and hover values.
- **Windows wrap past midnight.** In winter the cleanest block is overnight, so the window search
  runs over two copies of the typical day (`day_windows`, tested); a 23:00-02:00 block counts.

## 2026-10-19: audit fixes

- **Advice only from now on.** The dashboard showed the latest issue from its first half-hour,
  but that issue is as old as the last pipeline run (1-4 h), so the "cleanest window" could
  already have started. `upcoming` drops the past half-hours; if under 8 h of forecast is left
  (the pipeline is far behind) the page keeps the whole issue and says so.
- **Typical day weighted by days.** The greenest tab averaged the weekday and weekend means
  equally, counting weekends as half of all days and making every month look cleaner. Now
  weighted by half-hour counts (`typical_day`, tested).
- **No stale "last actual".** The latest actual was forward-filled without limit, so after a
  6 h publication outage the short-lead forecast copied a 6 h old value at full weight. It now
  bridges at most 2 h; beyond that the persistence blend switches off.
- **Missing weather variables fail.** A variable absent from an Open-Meteo reply used to load as
  NULLs the model would quietly ignore; the parser now raises.
- **Pinned DuckDB and LightGBM.** MotherDuck supports specific DuckDB versions, and the pipeline
  installed the newest each run; a new LightGBM would also change live forecasts unannounced.
- `model_version` is on `fct_model_vs_official` now, so the dashboard and `live_bias.py` no
  longer join `ml.model_forecasts` for it.
- **Review follow-ups.** The chart keeps the whole issue (trimming it left a gap after the last
  actual); only the advice uses the half-hours ahead, and its labels say how many hours that is
  ("next 47 h") instead of always "48 h", with a warning once the issue is over 12 h old. Lead
  bands are defined once (`BAND_EDGES`), with the SQL built from them and tested to match.
- **Backtest re-run** for the bounded last actual: the headline is unchanged (20.9 vs 44.3 for
  the best baseline); the no-NESO model moved 26.8 → 27.0. That figure was typed into the README
  by hand, so it now comes from `backtest_results.json` (`previous_model_mae`) like the others.

## 2026-10-19: Whole-repo review fixes

- **A NESO outage no longer stops the pipeline.** The forecast had a fallback for a stalled NESO
  feed, but it could never run: a NESO fetch error failed `gridcarbon incremental`, and a day-old
  NESO table failed `dbt source freshness`, so dbt and `predict` were skipped and the whole
  dashboard went stale. NESO datasets are now `optional`: a failure is logged, written to
  `meta.pipeline_runs.error` on a successful run, and printed; their freshness checks only warn.
- **Empty backfill windows are retried.** A window that came back empty was logged as loaded, so
  `backfill --resume` skipped it forever. Only windows with rows are logged now.
- **Missing NESO yearly archive fails loudly.** Early in January, before NESO publishes the new
  year's embedded archive, a window spanning the new year loaded only December and said nothing.
- **Dashboard without a forecast.** An empty `fct_model_vs_official` crashed the whole page; the
  Now and Planner tabs now say there is no forecast yet and the other tabs render. The accuracy
  example now really is the latest fully scored forecast (it used to pick the issue with the
  most actuals, which could be partly scored). The dashboard reads `MOTHERDUCK_DATABASE` like the
  pipeline instead of a hard-coded name.
- The backtest report said forecasts were issued every 6 h; it is 6.5 h (`ISSUE_EVERY`), and the
  text now comes from that constant. Generation-mix totals treat a missing fuel as 0% instead of
  going NULL (dbt unit test).

## 2026-10-19: neural net experiment (`analysis/nn_backtest.py`)

- **Question:** does a neural network beat LightGBM v3? A PyTorch MLP (3 x 256, L1 loss, 20
  epochs, settings fixed before the run) was trained on the same 12 folds, rows, 14 features
  and persistence blend, on a local RTX 3090 (~4 s per seed per fold). PyTorch is not a project
  dependency; the script's docstring has the install command.
- **Result: a tie on its own, a small win as an average.** The MLP (mean of 5 seeds per fold)
  scores MAE 20.5 vs LightGBM 20.9: -0.36, 95% interval -0.87 to +0.17, so not better. Single
  seeds land at -0.22 to -0.29, all with intervals crossing zero, so the seed hardly matters.
  The mean of LightGBM and the 5-seed MLP scores 20.3: -0.61 (-0.89 to -0.32), better at every
  lead band, and its greenest-3h-window regret is 6.25 vs 6.70. The two models make partly
  different errors, so averaging cancels some.
- **Caveats:** the MLP is untuned (~22 s per fold for 5 seeds on the GPU). The persistence
  blend was tuned for LightGBM. The ensemble is not live: it would need the pipeline to install
  PyTorch (CPU training time on GitHub Actions not yet measured) and a live track record first.
- **Decision: closed, no follow-up.** The gain is too small to be worth it: 0.45 gCO₂/kWh less
  greenest-window regret (about 3 g CO₂ on a 7 kWh EV charge), against a 3 GB dependency in the
  hourly run, a second model to maintain, and a backtest-to-live gap bigger than the gain. The
  script stays so it can be re-run in a few minutes if the inputs change a lot.

## 2026-10-19: wind speed at each wind-farm point (candidate `model_wind_points`)

- **Question:** the model sees GB wind as one average over 3 points. Does it do better when it
  sees forecast 100 m wind speed at each wind area as its own input and learns their weights
  itself, rather than us typing in capacities? 8 points were added to `WEATHER_POINTS`: 7
  offshore clusters (Moray Firth, Forth, Dogger Bank, Irish Sea, Lincs, East Anglia, Thames)
  and the Southern Uplands. That makes 11 wind inputs (`forecast.WIND_POINT_FEATURES`),
  including the 3 already averaged. The `cloud_cover_pct` average stays on the original 7 points
  so the existing series doesn't shift.
- **Backfill:** historical forecasts from 2022-01-01 for all 15 points (317,967 new rows). Every
  point has 42,064 hours with no nulls. A resume run skipped the new points at first, because
  their windows were already in `meta.load_log`, so it was re-run with `--no-resume`.
- **Result: better, and the interval is entirely below zero.** MAE was 18.7 vs 20.9 for the live
  model: -2.13, with a 95% interval of -2.76 to -1.50. It is better at every lead band (17.0 / 17.9 /
  18.4 / 19.6 vs 17.6 / 19.6 / 20.5 / 22.2) and its greenest-3h-window regret is 5.86 vs 6.70.
- **Caveat:** the gain grows with lead time, from 0.6 at 0-6 h to 2.6 at 24-48 h. Long leads are
  where Open-Meteo's historical forecasts flatter us most, because they are stitched from the
  first hours of each run. Expect the live gain at long leads to be smaller.
- **Decision: go, as a separate change.** It switches `FEATURES`, bumps the model version and
  backfills MotherDuck before the live model reads the new points. If a point's forecast is missing,
  the model falls back to NaN (LightGBM handles it) rather than failing the run.
- **Quota:** Open-Meteo bills each location separately, so only the historical forecast (which
  the model trains on) requests the wind-farm points. The hourly observed and live snapshot
  fetches stay on the original 7 (`GRID_POINTS`) until a live feature needs more.

## 2026-10-19: wind-farm points go live (`lgbm-direct-v4`)

- **Change:** `FEATURES` now includes the 11 wind-point inputs, so the live model is
  `lgbm-direct-v4`. For the live model to see them ahead of time, the live weather snapshot
  (`fetch_weather_forecast`) now requests all 15 points; only the observed fetch stays on the
  original 7. That adds 8 locations to each run's snapshot request.
- **Fallback, correcting the entry above:** a missing wind point does not go to LightGBM as NaN.
  Like a stalled NESO feed, it switches that run to the model on `BASE_FEATURES` only, labelled
  `lgbm-direct-v4-fallback`, because a model trained with every wind point present has never seen
  one missing. A test covers this.
- **MotherDuck:** backfilled with `--no-resume` from 2022-01-01 before the live model reads the
  points: every one of the 15 points has 42,064 hours up to the current hour.
- **Backtest (re-run):** v4 18.7, v3 20.9, v2 27.0. The v3 interval vs v4 is 1.50 to 2.76, all
  above zero, so v4 stays better. Bias over all leads rose from 1.3 (v3) to 2.0; it is small next to
  the MAE gain, but worth watching on the live track record.
- **Docs:** README phase 10 now shows v2 → v3 and phase 11 v3 → v4, using a new `v3_model_mae`
  marker in `backtest_results.json`, so each row keeps the gain it actually made.

## 2026-10-20: NESO embedded history was missing on MotherDuck

- **Problem:** the NESO embedded wind/solar archive (2022 onwards) was only ever loaded into the
  local warehouse, where the backtest runs. MotherDuck had snapshots since 15 Oct only, so in live
  training 0.2% of rows had those two inputs. Live v3 (from 18 Oct) and v4 (from 19 Oct 16:13 UTC)
  were therefore not the models the backtest scored. The pipeline log said so on every run
  ("0% of training rows have NESO inputs").
- **Fix:** `backfill -d neso_embedded_forecast --no-resume` on MotherDuck, 20 Oct 08:16-09:39 UTC:
  6,006,718 rows upserted. Rows per year now match the local warehouse for 2022-2025. Live training
  coverage of both inputs is 99.9%.
- **Track record:** live forecasts issued before 09:39 UTC on 20 Oct trained without full NESO
  history. Start any v4 live-vs-backtest comparison after that time.

## 2026-10-23: past forecasts on the Now chart

- **What:** the Now chart draws our model's forecast for the last 36 h (dashed orange) next to the
  actuals, from `marts.fct_model_vs_official`, which keeps every past issue.
- **Which forecast:** each half-hour has several, one per issue (a new one every ~6.5 h). We take
  the lead band with the lowest live MAE (the Forecast accuracy tab's numbers), newest issue first
  within it, and fall back to the next-best band where it has none. Picking per half-hour whichever
  forecast came closest to the actual would be hindsight and make the model look better than it is.
- **Our model only:** at 0-6 h the forward line shows NESO's numbers, but the past line shows
  ours, so it is a fair look at the model.

## 2026-10-23: NESO's past forecast on the Now chart too

- **What:** a dashed green line with NESO's forecast for the last 36 h, picked the same way as ours
  but ranked by NESO's own live MAE per lead band. Half-hours where an issue has no NESO value are
  skipped, so the next-best band fills them. Both past lines now sit side by side, so the chart
  compares the two forecasts directly; the earlier "our model only" choice no longer holds.

## 2026-10-26: first live check of v4, and the 22 Oct miss

**v4 against NESO, live.** `analysis/live_bias.py` on MotherDuck: 103 forecasts, 8,338 scored
half-hours over target days 19–26 Oct. Our MAE is 17.0, NESO's 21.7; bias +3.8 vs NESO's −14.6.
The backtest's worry (bias up from 1.3 to 2.0, long leads gaining less) half shows live: the
24–48 h band has our highest MAE (19.1 vs NESO's 22.2) and the highest bias (+5.2), but daily
bias swings both ways (−12 to +26), so it is not drift. v3 and v4 never ran in the same issue,
so there is no head-to-head; on the shared target days (19–21 Oct) both beat NESO by the same 3.5.
Too few days for the bootstrap interval (needs 45), so the README numbers are interim.

**22 Oct (our MAE 28.1, bias +25.9) was a weather-forecast miss, not a model bug.** The worst
forecasts were issued 20 Oct 09:00–17:00 at 36–48 h leads (bias +45 to +60). The 20 Oct 13:00
weather snapshot had mean 100 m wind at the points we also observe falling to 17–24 km/h over
00:00–15:00 UTC on 22 Oct; observed was 24–32. A calmer forecast means more gas, so we forecast
up to ~145 gCO₂/kWh for a morning that came in at ~110. The 21 Oct snapshots had the wind right
(within ~3 km/h) and our bias for those issues fell to about +12 to +28. Both we and NESO also
missed the late-evening drop (22:00, both over by ~50) as wind returned faster than forecast.
Nothing to fix in the model; it is the cost of 2-day-ahead wind forecasts.

## 2026-10-26: NESO's forecast on the Now chart for 3 h, not 6

- **What:** the forward line now shows NESO's forecast only for the first 3 h (`NESO_LEADS` 12 → 6
  half-hours), ours after that; legend and planner caption say so.
- **Why:** the 6 h hand-over came from v2/v3, when NESO beat us across 0-6 h. Live v4 (19-26 Oct,
  ~200 scored half-hours per hour of lead) splits that band: NESO is better for the first 3 h
  (MAE 10.2-15.3 vs our 14.1-16.2), we are better at 3-6 h (14.7-15.4 vs NESO's 16.8-19.8).
- **Caveat:** 8 days of data. Re-check the per-hour split on 8 Nov with the live write-up.

## 2026-10-26: Planner tab folded into "When is it greenest?"

- **What:** the Planner tab is gone. Its one answer ("start at X for the cleanest N h, vs starting
  now") now sits at the top of "When is it greenest?", driven by that tab's task-length slider.
- **Why:** two tabs asked the same question (when should I run a task of this length?) with two
  sliders; one answered from the live forecast, the other from 12-month averages. One tab with one
  slider shows both answers side by side.
