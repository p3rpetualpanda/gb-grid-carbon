select
    period_from,
    region_id,
    short_name,
    dno_region,
    forecast as forecast_gco2_kwh,
    intensity_index,
    region_id >= 15 as is_aggregate,  -- 15 England, 16 Scotland, 17 Wales, 18 GB
    generation_mix
from {{ source('raw', 'intensity_regional') }}
