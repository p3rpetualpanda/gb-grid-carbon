select
    region_id,
    max(short_name) as short_name,
    max(dno_region) as dno_region,
    bool_or(is_aggregate) as is_aggregate,
    case
        when region_id in (1, 2, 16) then 'Scotland'
        when region_id in (7, 17) then 'Wales'
        when region_id = 6 then 'Wales & England'  -- North Wales & Merseyside
        when region_id = 18 then 'GB'
        else 'England'
    end as country
from {{ ref('stg_intensity_regional') }}
group by region_id
