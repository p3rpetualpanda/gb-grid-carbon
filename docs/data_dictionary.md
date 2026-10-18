# Data dictionary

All facts below were verified against the live APIs on 2026-10-12 (see `docs/dev_log.md`).
**All timestamps are UTC.** Conversion to UK local time (`Europe/London`) happens only in dbt.

## Sources

### Carbon Intensity API (NESO), `https://api.carbonintensity.org.uk`
Free, no key. Licence: CC BY 4.0, credit "Carbon Intensity API, National Energy System Operator".

| Endpoint | Grain | Max range / request | History from (UTC) | Notes |
|---|---|---|---|---|
| `/intensity/{from}/{to}` | national, 30 min | 31 days (exactly 31 → 400) | 2017-09-11 23:00 | `forecast`, `actual` (gCO₂/kWh), `index` |
| `/generation/{from}/{to}` | national, 30 min | 60 days (exactly 60 OK) | 2018-05-10 23:30 | % share by fuel (9 fuels) |
| `/regional/intensity/{from}/{to}` | 18 regions, 30 min | 14 days (exactly 14 → 400) | 2018-05-10 23:30 | forecast only, **no actuals**; ~3 MB JSON per 10 days |
| `/intensity/{from}/fw48h` | national, 30 min | n/a | live | official 48 h forward forecast (our benchmark) |

**Window semantics:** a request for `[from, to]` returns every period whose *end* lies in
`[from, to]`, so it includes the period finishing exactly at `from` (e.g. asking from 00:00 also
returns 23:30–00:00). The extractors filter to `period_from ∈ [from, to)`, so adjacent windows
neither overlap nor leave gaps (`tests/test_carbon_intensity.py` asserts this).

**Year-end truncation:** `/generation` and `/regional` return HTTP 200 but silently stop at
31 Dec 23:00 when a request spans a year boundary. Windows are split at 1 January.

**Known upstream gaps** (after backfill): national intensity 0.11% of half-hours missing,
generation mix 0.29%. 626 intensity periods have no `actual`. Fuel shares sum to 99.7–100.3%.

Timestamp format: `YYYY-MM-DDTHH:MMZ` (no seconds), e.g. `2024-01-01T00:30Z`.

`index` values: `very low`, `low`, `moderate`, `high`, `very high`.

Fuels: `biomass, coal, imports, gas, nuclear, other, hydro, solar, wind`.

Regions (`regionid`): 1–14 are the DNO licence areas (1 North Scotland … 13 London, 14 South East
England); **15 England, 16 Scotland, 17 Wales, 18 GB** are aggregates. Never sum all 18.

### Open-Meteo, `https://open-meteo.com`
Free, no key, non-commercial use, CC BY 4.0. Requested with `timezone=GMT` (`utc_offset_seconds`
must be 0; the schema enforces it). Several locations per call (comma-separated lat/lon).

| API | Use | Range used |
|---|---|---|
| `archive-api.open-meteo.com/v1/archive` | observed (reanalysis) weather for analysis | 2017-09-11 → now |
| `historical-forecast-api.open-meteo.com/v1/forecast` | forecasts *as issued*: leakage-safe model training | 2022-01-01 → now |
| `api.open-meteo.com/v1/forecast` | live forecast snapshot (model input) | next 3 days |

Hourly variables: `temperature_2m` (°C), `wind_speed_100m` (km/h), `shortwave_radiation` (W/m²),
`cloud_cover` (%). `end_date` is **inclusive**; extractors convert to half-open windows.

Weather points (`src/gridcarbon/extract/weather.py`):

| point_id | lat, lon | why |
|---|---|---|
| north_scotland | 57.48, -4.22 | onshore wind + hydro |
| south_scotland | 55.86, -4.25 | onshore wind, demand |
| north_sea_offshore | 53.90, 1.80 | offshore wind (Dogger/Hornsea area) |
| manchester | 53.48, -2.24 | demand |
| birmingham | 52.49, -1.89 | demand |
| london | 51.51, -0.13 | demand, solar |
| south_england_solar | 51.06, -1.80 | solar farms |

### NESO Data Portal, `https://api.neso.energy/api/3/action` (CKAN)
Free, no key. Licence: NESO Open Data Licence. Rows are read with `datastore_search_sql`, filtered
on each forecast's own timestamp, and only targets up to 3 days after issue are kept.

| Dataset | Grain | History used | Notes |
|---|---|---|---|
| `day-ahead-wind-forecast` | half-hour × forecast vintage, transmission-connected wind (MW) | 2022 → now | Since mid-2026 each vintage is stamped 08:45 and covers that day only. The archive has empty weeks: Jun–Sep 2023, Oct–Nov 2025 (4 weeks), Jan 2026 (1) and May 2026 (5). |
| `14-days-ahead-wind-forecasts` | half-hour, transmission-connected wind (MW), **latest vintage only** | snapshotted each run from 18 Oct 2026 | `Datetime` and `ForecastDateTime` are UTC (checked across the October clock change: 50 settlement periods). |
| `embedded-wind-and-solar-forecasts` | half-hour × vintage, wind and solar on local networks (MW) | 2022 → now | `TIME_GMT` is the **end** of the half-hour. Only the yearly archives carry the issue time. |

## Warehouse: `raw` schema

Every raw table also has `_ingested_at TIMESTAMPTZ` and `_run_id VARCHAR` (lineage).
Loads are upserts on the natural key (in bold).

| Table | Columns |
|---|---|
| `raw.intensity_national` | **period_from**, period_to, forecast INT, actual INT (nullable: arrives late), intensity_index |
| `raw.generation_mix` | **period_from, fuel**, period_to, perc DOUBLE |
| `raw.intensity_regional` | **period_from, region_id**, period_to, short_name, dno_region, forecast INT, intensity_index, generation_mix (JSON text: `[{"fuel":"wind","perc":47.4},…]`) |
| `raw.official_forecast_snapshots` | **issued_at, period_from**, period_to, forecast INT, intensity_index |
| `raw.weather_observed` | **time, point_id**, latitude, longitude, temperature_2m, wind_speed_100m, shortwave_radiation, cloud_cover |
| `raw.weather_historical_forecast` | same as `weather_observed` |
| `raw.weather_forecast_snapshots` | **issued_at, time, point_id**, + weather columns |
| `raw.neso_wind_forecast` | **issued_at, period_from**, forecast_mw, capacity_mw |
| `raw.neso_wind_14d_snapshots` | **issued_at, period_from**, forecast_mw, capacity_mw |
| `raw.neso_embedded_forecast` | **issued_at, period_from**, wind_mw, wind_capacity_mw, solar_mw, solar_capacity_mw |

`issued_at` = the half-hour (official) or hour (weather) in which *we* fetched the forecast. NESO
does not publish the exact issue time, so this is the honest "what was known at that moment".

## Warehouse: `meta` schema

| Table | Purpose |
|---|---|
| `meta.pipeline_runs` | one row per CLI run: run_id, command, started/finished, status (`running`/`success`/`failed`), rows_loaded, error |
| `meta.load_log` | one row per loaded window: dataset, window_start, window_end, rows; makes backfills resumable |
