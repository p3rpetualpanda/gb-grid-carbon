-- Every half-hour from the first to the last loaded period: joining to it exposes gaps.
-- coalesce keeps the model valid on an empty warehouse (CI).
select generate_series as period_from
from generate_series(
    coalesce((select min(period_from) from {{ ref('stg_intensity_national') }}),
             timestamptz '2017-09-11 23:00:00+00'),
    coalesce((select max(period_from) from {{ ref('stg_intensity_national') }}),
             timestamptz '2017-09-11 23:00:00+00'),
    interval 30 minute
)
