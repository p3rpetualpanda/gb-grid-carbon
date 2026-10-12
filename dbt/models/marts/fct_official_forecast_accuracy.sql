select
    f.issued_at,
    f.period_from,
    f.lead_hours,
    f.forecast_gco2_kwh,
    i.actual_gco2_kwh,
    f.forecast_gco2_kwh - i.actual_gco2_kwh as error_gco2_kwh
from {{ ref('stg_official_forecast') }} as f
inner join {{ ref('stg_intensity_national') }} as i on i.period_from = f.period_from
where i.actual_gco2_kwh is not null
