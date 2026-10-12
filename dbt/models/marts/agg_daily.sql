select
    date_local,
    count(actual_gco2_kwh) as n_periods_with_actual,
    avg(actual_gco2_kwh) as mean_actual_gco2_kwh,
    min(actual_gco2_kwh) as min_actual_gco2_kwh,
    max(actual_gco2_kwh) as max_actual_gco2_kwh,
    arg_min(halfhour_slot, actual_gco2_kwh) as greenest_halfhour_slot,
    avg(renewable_pct) as mean_renewable_pct,
    avg(low_carbon_pct) as mean_low_carbon_pct,
    avg(fossil_pct) as mean_fossil_pct
from {{ ref('fct_grid_halfhourly') }}
group by date_local
