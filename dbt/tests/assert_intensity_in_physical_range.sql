-- GB intensity has never been outside 0-1000 gCO2/kWh. Staging nulls known-bad upstream
-- forecasts (is_forecast_invalid), so anything left here is a new parsing/unit bug. Recent data
-- only, so one old bad row can't fail every later build.
select period_from, forecast_gco2_kwh, actual_gco2_kwh
from {{ ref('stg_intensity_national') }}
where period_from >= (select max(period_from) - interval 14 day from {{ ref('stg_intensity_national') }})
  and (forecast_gco2_kwh not between 0 and 1000 or actual_gco2_kwh not between 0 and 1000)
