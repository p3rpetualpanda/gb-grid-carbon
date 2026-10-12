-- Fuel shares for each half-hour should add up to ~100% (rounding allowed).
select period_from, sum(share_pct) as total_pct
from {{ ref('stg_generation_mix') }}
group by period_from
having abs(sum(share_pct) - 100) > 1.5
