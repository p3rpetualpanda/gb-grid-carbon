-- Recent half-hours missing from the API. Warn only: upstream gaps are outside our control,
-- but a burst of them means the pipeline (or the API) needs attention.
{{ config(severity='warn') }}

select period_from
from {{ ref('fct_grid_halfhourly') }}
where is_missing
  and period_from >= (select max(period_from) - interval 7 day from {{ ref('fct_grid_halfhourly') }})
