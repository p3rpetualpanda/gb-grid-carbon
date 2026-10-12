select
    period_from,
    region_id,
    forecast_gco2_kwh,
    intensity_index,
    is_aggregate
from {{ ref('stg_intensity_regional') }}
