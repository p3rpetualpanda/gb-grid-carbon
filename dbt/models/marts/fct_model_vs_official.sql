-- Live track record: our forecast and NESO's, issued in the same pipeline run, scored against
-- the actual once it is published. Rows without an actual yet are kept (dashboard shows them).
select
    m.issued_at,
    m.period_from,
    m.lead,
    m.model_gco2_kwh,
    o.forecast_gco2_kwh as neso_gco2_kwh,
    i.actual_gco2_kwh,
    abs(m.model_gco2_kwh - i.actual_gco2_kwh) as model_abs_error,
    abs(o.forecast_gco2_kwh - i.actual_gco2_kwh) as neso_abs_error
from {{ ref('stg_model_forecasts') }} as m
left join {{ ref('stg_official_forecast') }} as o
    on o.issued_at = m.issued_at and o.period_from = m.period_from
left join {{ ref('stg_intensity_national') }} as i
    on i.period_from = m.period_from
