-- GB-level hourly weather: each variable averaged over the points where it drives the grid
-- (point roles: src/gridcarbon/extract/weather.py WEATHER_POINTS).
select
    hour_utc,
    avg(wind_speed_100m_kmh) filter (
        where point_id in ('north_scotland', 'south_scotland', 'north_sea_offshore')
    ) as wind_speed_100m_kmh,
    avg(solar_radiation_wm2) filter (
        where point_id in ('london', 'south_england_solar')
    ) as solar_radiation_wm2,
    avg(temperature_c) filter (
        where point_id in ('manchester', 'birmingham', 'london')
    ) as temperature_c,
    avg(cloud_cover_pct) as cloud_cover_pct
from {{ ref('stg_weather_observed') }}
group by hour_utc
