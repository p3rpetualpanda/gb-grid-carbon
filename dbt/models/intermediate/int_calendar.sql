-- UK local-time attributes. Everything upstream is UTC; this is the only place we convert.
with spine as (
    select
        period_from,
        timezone('Europe/London', period_from) as local_ts
    from {{ ref('int_periods_spine') }}
)

select
    s.period_from,
    s.local_ts,
    cast(s.local_ts as date) as date_local,
    hour(s.local_ts) as hour_local,
    hour(s.local_ts) * 2 + (minute(s.local_ts) >= 30)::int as halfhour_slot,  -- 0..47 local
    isodow(s.local_ts) as iso_weekday,
    isodow(s.local_ts) >= 6 as is_weekend,
    h.holiday_date is not null as is_bank_holiday,
    year(s.local_ts) as year,
    month(s.local_ts) as month,
    case
        when month(s.local_ts) in (12, 1, 2) then 'winter'
        when month(s.local_ts) in (3, 4, 5) then 'spring'
        when month(s.local_ts) in (6, 7, 8) then 'summer'
        else 'autumn'
    end as season
from spine as s
left join {{ ref('uk_bank_holidays') }} as h
    on h.holiday_date = cast(s.local_ts as date)
