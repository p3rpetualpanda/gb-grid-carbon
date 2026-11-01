-- Half-hours in the last week (to a day before the latest) with fewer than the 18 regions: 14 DNO
-- regions plus England, Scotland, Wales and GB. A missing region leaves no row to test, so count
-- them. Old history has different coverage, so the window is bounded. Warn only.
{{ config(severity='warn') }}

with latest as (select max(period_from) as t from {{ ref('fct_regional_halfhourly') }})

select period_from, count(distinct region_id) as n_regions
from {{ ref('fct_regional_halfhourly') }}, latest
where period_from between latest.t - interval 7 day and latest.t - interval 1 day
group by period_from
having count(distinct region_id) < 18
