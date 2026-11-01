-- Fuel shares for each half-hour should add up to ~100% (rounding allowed). Recent data only: an
-- error on one old bad row would fail every later build and stop the pipeline before `predict`.
select period_from, sum(share_pct) as total_pct
from {{ ref('stg_generation_mix') }}
where period_from >= (select max(period_from) - interval 14 day from {{ ref('stg_generation_mix') }})
group by period_from
having abs(sum(share_pct) - 100) > 1.5
