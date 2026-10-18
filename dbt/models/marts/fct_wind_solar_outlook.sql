-- NESO's newest wind and solar forecast for each half-hour (the dashboard's outlook chart).
-- Wind farms on the transmission network: the newest of the day-ahead archive (one vintage a
-- day, covering that day) and the 14-day forecast we snapshot each run (covers the full 48 h).
-- Wind and solar on local networks come from the 0-14 day embedded forecast.
with wind as (
    select
        period_from,
        arg_max(forecast_mw, issued_at) as wind_mw,
        max(issued_at) as wind_issued_at
    from (
        select period_from, forecast_mw, issued_at
        from {{ source('raw', 'neso_wind_forecast') }}
        union all
        select period_from, forecast_mw, issued_at
        from {{ source('raw', 'neso_wind_14d_snapshots') }}
    )
    group by period_from
),

embedded as (
    select
        period_from,
        arg_max(wind_mw, issued_at) as embedded_wind_mw,
        arg_max(solar_mw, issued_at) as embedded_solar_mw,
        max(issued_at) as embedded_issued_at
    from {{ source('raw', 'neso_embedded_forecast') }}
    group by period_from
)

select
    period_from,
    wind.wind_mw,
    embedded.embedded_wind_mw,
    embedded.embedded_solar_mw,
    wind.wind_issued_at,
    embedded.embedded_issued_at
from wind
full outer join embedded using (period_from)
