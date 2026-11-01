-- Half-hours with national intensity but no generation mix, and hours well past the weather
-- archive's ~2 day lag with no weather. Warn only, like the intensity gap test.
{{ config(severity='warn') }}

with latest as (select max(period_from) as t from {{ ref('fct_grid_halfhourly') }})

select period_from, is_missing_mix, is_missing_weather
from {{ ref('fct_grid_halfhourly') }}, latest
where (is_missing_mix and period_from between latest.t - interval 7 day and latest.t - interval 1 day)
   or (is_missing_weather and period_from between latest.t - interval 14 day and latest.t - interval 4 day)
