select
    period_from,
    fuel,
    perc as share_pct
from {{ source('raw', 'generation_mix') }}
