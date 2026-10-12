select
    issued_at,
    period_from,
    forecast as forecast_gco2_kwh
from {{ source('raw', 'official_forecast_snapshots') }}
