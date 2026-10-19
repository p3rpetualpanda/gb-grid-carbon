with wide as (
    select
        period_from,
        sum(share_pct) filter (where fuel = 'gas') as gas_pct,
        sum(share_pct) filter (where fuel = 'coal') as coal_pct,
        sum(share_pct) filter (where fuel = 'nuclear') as nuclear_pct,
        sum(share_pct) filter (where fuel = 'wind') as wind_pct,
        sum(share_pct) filter (where fuel = 'solar') as solar_pct,
        sum(share_pct) filter (where fuel = 'hydro') as hydro_pct,
        sum(share_pct) filter (where fuel = 'biomass') as biomass_pct,
        sum(share_pct) filter (where fuel = 'imports') as imports_pct,
        sum(share_pct) filter (where fuel = 'other') as other_pct
    from {{ ref('stg_generation_mix') }}
    group by period_from
)

-- A fuel missing from a period counts as 0% in the totals (else one gap NULLs the whole total).
select
    *,
    coalesce(wind_pct, 0) + coalesce(solar_pct, 0) + coalesce(hydro_pct, 0)
        as renewable_pct,  -- biomass and imports deliberately excluded
    coalesce(wind_pct, 0) + coalesce(solar_pct, 0) + coalesce(hydro_pct, 0)
        + coalesce(nuclear_pct, 0) as low_carbon_pct,
    coalesce(gas_pct, 0) + coalesce(coal_pct, 0) as fossil_pct
from wide
