# Brief: cutting the carbon of EV charging by changing *when*, not *how much*

**For:** Head of Product, a UK smart-charging app (illustrative client) · **From:** data analyst ·
**Basis:** 9 years of half-hourly GB grid data (Sept 2017 – Oct 2026); method and code in this repo

## Bottom line

Moving a typical overnight charge from "start when plugged in at 6 pm" to "the cleanest 3 hours
before 7 am" cuts its emissions by **29%** (95% CI 26–32%): about **113 kg CO₂ per car per year**
(101–124 kg), at no cost and no change to how much the driver charges. We already have the
forecast needed to do it automatically.

## What the data shows

1. **Timing beats almost everything else a driver controls.** Over the last 12 months, electricity
   at 03:00 was 26% cleaner than at 18:00 (95% CI 22–31%). The early-evening peak is the dirtiest
   time of day all year round.
2. **From spring to autumn, midday is now the cleanest time.** Solar has moved the cleanest
   half-hour to around 12:00 (101 gCO₂/kWh on average vs 153 at 19:00). This is new: it wasn't
   true a few years ago, and many "charge overnight" rules of thumb haven't caught up.
3. **Wind drives day-to-day swings, so they can be forecast.** Each +10 km/h of wind at wind-farm
   locations comes with about 35 gCO₂/kWh lower intensity (95% CI 32–37). Our 48-hour forecast,
   which uses weather forecasts, is 40% more accurate than assuming "same as yesterday" over a
   12-month backtest.

## Recommendations

| # | Recommendation | Expected impact | Effort |
|---|---|---|---|
| 1 | **Make "cleanest window" the default overnight schedule**, chosen daily from the 48 h forecast, still finishing by the driver's departure time. | ~29% lower charging emissions; ~113 kg CO₂/car/year; ~113,000 t/year per million cars | Low: forecast and window-finder already exist (dashboard "Planner") |
| 2 | **Offer a "solar window" for daytime-parked cars, March–September** (workplace or home-working drivers): charge around midday. | Uses the cleanest hours of the year; reduces evening-peak demand | Medium: needs daytime plug-in behaviour data |
| 3 | **Show drivers the saving in their own terms** ("tonight's smart charge avoided 0.3 kg CO₂"), using the dashboard's numbers. | Retention and opt-in; makes the green claim verifiable | Low |

**How to measure it:** A/B test the default (recommendation 1) on a sample of users for 4 weeks.
Primary metric: emissions per kWh charged. Guardrail: share of cars not fully charged by departure.

## How sure are we?

- Every headline number has a 95% confidence interval from a block bootstrap over whole weeks, so
  weather spells aren't treated as independent days. All intervals exclude zero.
- Data quality is strong: 0.11% of half-hours are missing upstream, and 16 impossible published
  forecasts were found and excluded by automated tests.

## Limitations (read before quoting)

- **Average vs marginal emissions.** These figures use *average* grid intensity. The emissions
  actually caused by *extra* demand depend on which power station responds (often gas), so real
  savings could differ. A marginal-emissions analysis is the natural next step before any public
  claim.
- **Scenario assumptions:** 7 kWh per night, a 3-hour charge, plug-in at 18:00, every night. Real
  behaviour varies; the A/B test above measures the real effect.
- **Regional differences are large** (South Scotland ~8 vs South Wales ~252 gCO₂/kWh on average),
  but NESO publishes only regional *forecasts*, so region-specific savings aren't estimated here.
- **Forecast accuracy:** the model beats naïve baselines, but the grid operator's own short-range
  forecast is still more accurate in the backtest. Its live head-to-head record is building in the
  dashboard.

_Sources: [`findings.md`](findings.md) (all figures and charts), [`backtest.md`](backtest.md)
(forecast evaluation), [live dashboard](https://gb-grid-carbon-b8k4zy3yzvfzqzxewxzdsk.streamlit.app/)._
