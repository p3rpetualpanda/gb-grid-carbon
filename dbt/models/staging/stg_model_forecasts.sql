select
    issued_at,
    period_from,
    lead,
    yhat as model_gco2_kwh,
    model_version,
    -- Stage-1 forecast before any later correction step (NULL for runs before it was stored).
    yhat_base as base_gco2_kwh,
    base_version
from {{ source('ml', 'model_forecasts') }}
