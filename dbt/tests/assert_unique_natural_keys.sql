-- Loads upsert on these keys, so duplicates should be impossible; this proves it in the warehouse.
select 'generation_mix' as tbl, period_from, fuel as key_part
from {{ ref('stg_generation_mix') }}
group by all
having count(*) > 1

union all

select 'intensity_regional', period_from, cast(region_id as varchar)
from {{ ref('stg_intensity_regional') }}
group by all
having count(*) > 1
