select
    time as hour_utc,
    point_id,
    temperature_2m as temperature_c,
    wind_speed_100m as wind_speed_100m_kmh,
    shortwave_radiation as solar_radiation_wm2,
    cloud_cover as cloud_cover_pct
from {{ source('raw', 'weather_observed') }}
