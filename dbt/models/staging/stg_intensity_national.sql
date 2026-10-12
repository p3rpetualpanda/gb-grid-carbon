select
    period_from,
    period_to,
    -- 16 upstream forecasts in 2018-19 are impossible (up to 13,579) while actuals are normal:
    -- null them so they cannot distort forecast-accuracy analysis; raw keeps the original.
    case when forecast between 0 and 1000 then forecast end as forecast_gco2_kwh,
    forecast is not null and forecast not between 0 and 1000 as is_forecast_invalid,
    actual as actual_gco2_kwh,
    intensity_index,
    _ingested_at
from {{ source('raw', 'intensity_national') }}
