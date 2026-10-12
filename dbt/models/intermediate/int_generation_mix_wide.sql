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

select
    *,
    wind_pct + solar_pct + hydro_pct as renewable_pct,  -- biomass and imports deliberately excluded
    wind_pct + solar_pct + hydro_pct + nuclear_pct as low_carbon_pct,
    gas_pct + coal_pct as fossil_pct
from wide
