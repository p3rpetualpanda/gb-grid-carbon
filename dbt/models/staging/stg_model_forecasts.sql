select
    issued_at,
    period_from,
    lead,
    yhat as model_gco2_kwh,
    model_version
from {{ source('ml', 'model_forecasts') }}
