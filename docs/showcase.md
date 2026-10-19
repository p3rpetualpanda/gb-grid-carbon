# Showcasing this project

Material for the CV, LinkedIn and interviews. Every number comes from `docs/findings.md`,
`docs/backtest.md` or `docs/dev_log.md`; re-check them after re-running the analysis scripts.

## CV bullets (pick 2–3)

- Built an automated ELT pipeline (Python, GitHub Actions, dbt, DuckDB/MotherDuck) that loads
  half-hourly GB grid carbon-intensity, generation-mix and weather data every few hours (2.6M+ rows
  since 2017), with idempotent upserts, resumable backfills and 24 dbt data tests.
- Found and fixed silent data loss through data-quality checks: an API that drops all data after
  31 December in year-spanning requests (missing data 10.4% → 0.29%), and 16 impossible upstream
  forecasts caught by a dbt range test.
- Developed a 48-hour LightGBM carbon-intensity forecast with leakage-safe features and a 12-month
  rolling-origin backtest; MAE <!-- bt:model_mae -->18.7<!-- /bt --> gCO₂/kWh, <!-- bt:improvement -->58%<!-- /bt --> better than naïve baselines, and scored live
  against the system operator's forecast every pipeline run.
- Analysed 9 years of grid data with bootstrap confidence intervals: intensity fell 48% (2018–2025),
  midday is now the cleanest time from spring to autumn, and smart EV charging cuts charging
  emissions by 29% (95% CI 26–32%). Wrote a one-page stakeholder brief with recommendations and an
  A/B test design, and published a live Streamlit dashboard with a "when should I run it?" planner,
  the live generation mix, NESO's wind and solar outlook and a plain-English forecast track record.

## LinkedIn post (draft)

> I built a pipeline that answers a simple question: when is Great Britain's electricity cleanest?
>
> Every few hours it pulls grid and weather data, tests it with dbt, forecasts the next 48 hours and
> updates a live dashboard. Three things I learned:
>
> 1. Midday is now the cleanest time of day for most of the year, thanks to solar. Evenings are
>    dirtiest all year round.
> 2. "No errors" doesn't mean "complete data". The API silently dropped every January from my
>    backfill. A gap check against a complete time spine found it.
> 3. Charging an EV in the cleanest 3 hours overnight instead of at 6pm cuts its emissions by about
>    29%, roughly 113 kg CO₂ per car per year, at no cost to the driver.
>
> Code, findings and dashboard: https://github.com/p3rpetualpanda/gb-grid-carbon
> #dataanalytics #dbt #python #energy

(Attach `docs/img/time_of_day_heatmap.png`.)

## Interview questions you should be ready for

**Walk me through your recommendations.** Use [`brief.md`](brief.md): bottom line first (29%
lower charging emissions by changing *when*), three findings, three recommendations with impact,
how you'd measure it (A/B test on the default schedule), then the limitations.

**How do you know the 29% isn't chance?** 95% block-bootstrap interval (26–32%): resample whole
weeks so weather spells stay together, recompute 1,000 times. The interval excludes zero.

**What's the biggest weakness in your analysis?** Average vs marginal emissions: extra demand is
usually met by gas, so real savings from shifting load could differ. It's the first thing to
check before a public claim.

**Why dbt? What's the difference between staging and marts?**
Staging renames and types one source each (views); intermediate builds reusable pieces (time spine,
UK calendar, wide mix); marts are the tables people query. dbt gives version-controlled SQL,
dependency order, tests and docs/lineage for free.

**How is the pipeline idempotent? How do you handle late data?**
Every load is an upsert on the natural key inside a transaction, so re-running changes nothing
and an empty API response can't delete history. Actual intensity is published late, so each run
re-fetches from (latest row − 3 days); anchoring on the latest row means it catches up after
an outage.

**What happens if the API is down for a day?**
Retries with backoff for transient errors; if the run still fails it's logged in
`meta.pipeline_runs` and GitHub emails me. The next run's look-back window fills the gap, and dbt
source freshness fails the build after 24 h without new data.

**How did you avoid leakage in the forecast?**
Features only use data known at issue time: actuals stop 1 h before (publication lag), "same
slot" lags step back as many days as needed, and weather comes from historical *forecasts*,
not observations. The backtest retrains each month only on targets before that month. Tests
enforce all three.

**Your model loses to NESO in the backtest. Why is that OK?**
The API only keeps NESO's final forecast, likely made shortly before the half-hour, and NESO also
has generation schedules I don't. So the backtest comparison flatters NESO at long leads. The fair
test is the live track record: both forecasts issued in the same run. My model's job is to beat
naïve baselines (<!-- bt:improvement -->58%<!-- /bt --> better) and to be honest about where it stands.

**Tell me about a bug you found.**
On day one the live forecast said ~170 when the last actual was 88 (the truth was 86). Two causes:
a train/serve skew (training issue times only covered 4 times of day, fixed with a 6.5 h grid
that cycles through all 48 half-hours) and under-use of the latest actual at very short leads
(fixed with a persistence blend tuned on half the backtest and validated on the other half).

**What would you change for a real company / 100× the data?**
Incremental dbt models, an orchestrator with retries and alerting (Dagster/Airflow), a cloud
warehouse with separate dev/prod environments, a stored and versioned model with drift
monitoring, and NESO's own forecast as a model feature once enough snapshots have accumulated.
