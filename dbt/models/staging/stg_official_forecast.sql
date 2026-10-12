select
    issued_at,
    period_from,
    forecast as forecast_gco2_kwh,
    date_diff('minute', issued_at, period_from) / 60.0 as lead_hours
from {{ source('raw', 'official_forecast_snapshots') }}
