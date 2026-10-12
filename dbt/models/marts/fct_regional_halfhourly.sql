select
    period_from,
    cast(timezone('Europe/London', period_from) as date) as date_local,
    region_id,
    forecast_gco2_kwh,
    intensity_index,
    is_aggregate
from {{ ref('stg_intensity_regional') }}
