-- Once the pipeline starts storing the stage-1 forecast (yhat_base), every later run must store
-- it too: a gap would leave error features computed from corrected forecasts. Earlier runs are
-- allowed to be NULL. Warn, don't fail: history is never rewritten, so one stray run would
-- otherwise fail every later dbt build and stop the pipeline before `predict` runs.
{{ config(severity='warn') }}

select issued_at, period_from
from {{ ref('stg_model_forecasts') }}
where issued_at >= (
        select min(issued_at) from {{ ref('stg_model_forecasts') }} where base_gco2_kwh is not null
    )
    and (base_gco2_kwh is null or base_version is null)
