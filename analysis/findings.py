"""Phase 7: headline findings from the dbt marts -> docs/findings.md + docs/img/*.png.

Run after `dbt build` (local warehouse):  python analysis/findings.py
Every number in the report is computed here, so re-running refreshes the whole write-up.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"

# Reference palette (light): one categorical blue for single-series charts, blue ramp for the heatmap.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED, GRID, AXIS, SURFACE = (
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
    "#fcfcfb",
)
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

con = duckdb.connect(str(ROOT / "data" / "gridcarbon.duckdb"), read_only=True)
con.execute("SET TimeZone = 'UTC'")


def q(sql: str):
    return con.sql(sql).df()


def chart(title: str, ylabel: str = "", size=(8, 4)):
    fig, ax = plt.subplots(figsize=size, dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, pad=12)
    ax.set_ylabel(ylabel, color=INK2)
    ax.tick_params(colors=MUTED, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    return fig, ax


def save(fig, name: str) -> str:
    IMG.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(IMG / name, facecolor=SURFACE)
    plt.close(fig)
    return f"img/{name}"


RNG = np.random.default_rng(7)  # fixed seed: the report regenerates identically


def block_ci(df, stat, reps: int = 1000, strata: str | None = None) -> tuple[float, float]:
    """95% bootstrap CI of stat(df), resampling whole weeks (column `wk`) with replacement.

    Whole weeks keep multi-day weather spells together; resampling single half-hours would
    treat correlated hours as independent and give falsely narrow intervals."""
    parts = [df] if strata is None else [g for _, g in df.groupby(strata)]
    weeks = [[g for _, g in p.groupby("wk")] for p in parts]
    draws = [
        stat(pd.concat([w[i] for w in weeks for i in RNG.integers(0, len(w), len(w))]))
        for _ in range(reps)
    ]
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(lo), float(hi)


def ci(lo: float, hi: float, fmt: str = ".0%") -> str:
    return f"95% CI {lo:{fmt}} to {hi:{fmt}}"


LAST_12M = (
    "period_from > (select max(period_from) from marts.fct_grid_halfhourly) - interval 365 day"
)
F: dict[str, str] = {}  # finding name -> formatted markdown

# 1. Long-term trend -------------------------------------------------------------------------
annual = q("""
    select year, avg(actual_gco2_kwh) as mean_g, avg(gas_pct) as gas, avg(wind_pct) as wind,
           avg(coal_pct) as coal
    from marts.fct_grid_halfhourly where year between 2018 and 2025 group by year order by year
""")
first, last = annual.iloc[0], annual.iloc[-1]
drop = 1 - last.mean_g / first.mean_g
fig, ax = chart("Average GB grid carbon intensity by year", "gCO₂/kWh")
ax.bar(annual.year, annual.mean_g, color=BLUE, width=0.6)
for _, r in annual.iloc[[0, -1]].iterrows():
    ax.annotate(f"{r.mean_g:.0f}", (r.year, r.mean_g), ha="center", va="bottom", color=INK2,
                xytext=(0, 3), textcoords="offset points")  # fmt: skip
ax.set_xticks(annual.year)
img_trend = save(fig, "annual_intensity.png")

mix = annual[annual.year >= 2019]
fig, ax = chart("Share of GB generation: gas vs wind", "% of generation")
for col, colour, label in (("gas", ORANGE, "Gas"), ("wind", BLUE, "Wind")):
    ax.plot(mix.year, mix[col], color=colour, linewidth=2, marker="o", markersize=5, label=label)
    ax.annotate(label, (mix.year.iloc[-1], mix[col].iloc[-1]), xytext=(6, 0),
                textcoords="offset points", va="center", color=INK2)  # fmt: skip
ax.legend(frameon=False, loc="upper center", ncol=2, labelcolor=INK2)
ax.set_xticks(mix.year)
img_mix = save(fig, "gas_vs_wind.png")
ends = q(f"""
    select year, date_trunc('week', date_local) as wk, actual_gco2_kwh as y
    from marts.fct_grid_halfhourly
    where year in ({first.year:.0f}, {last.year:.0f}) and actual_gco2_kwh is not null
""")
drop_ci = block_ci(
    ends,
    lambda s: 1 - s.y[s.year == last.year].mean() / s.y[s.year == first.year].mean(),
    strata="year",
)
F["trend"] = (
    f"Average carbon intensity fell **{drop:.0%}** ({ci(*drop_ci)}), from **{first.mean_g:.0f}** "
    f"gCO₂/kWh in {first.year:.0f} to **{last.mean_g:.0f}** in {last.year:.0f}. Wind's share of generation "
    f"went from {mix.wind.iloc[0]:.0f}% (2019) to {mix.wind.iloc[-1]:.0f}% ({mix.year.iloc[-1]:.0f}), "
    f"gas from {mix.gas.iloc[0]:.0f}% to {mix.gas.iloc[-1]:.0f}%, and coal from "
    f"{annual.coal.iloc[1]:.1f}% to {annual.coal.iloc[-1]:.1f}%."
)

# 2. Time of day (last 12 months) -------------------------------------------------------------
slots = q(f"""
    select halfhour_slot, avg(actual_gco2_kwh) as g from marts.fct_grid_halfhourly
    where {LAST_12M} group by 1 order by 1
""")
best, worst = slots.loc[slots.g.idxmin()], slots.loc[slots.g.idxmax()]


def hhmm(slot: float) -> str:
    return f"{int(slot) // 2:02d}:{30 * (int(slot) % 2):02d}"


s3am = slots.g[slots.halfhour_slot == 6].iloc[0]
s6pm = slots.g[slots.halfhour_slot == 36].iloc[0]
heat = q(f"""
    select month, hour_local, avg(actual_gco2_kwh) as g from marts.fct_grid_halfhourly
    where {LAST_12M} group by all
""").pivot(index="month", columns="hour_local", values="g")
fig, ax = chart("When is GB electricity cleanest? (last 12 months)", "", size=(9, 4.2))
ax.grid(False)
cmap = matplotlib.colors.LinearSegmentedColormap.from_list("seq_blue", SEQ_BLUE)
im = ax.imshow(heat.values, aspect="auto", cmap=cmap)
ax.set_yticks(range(12), ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                          "Nov", "Dec"])  # fmt: skip
ax.set_xticks(range(0, 24, 3), [f"{h:02d}:00" for h in range(0, 24, 3)])
ax.set_xlabel("UK local time", color=INK2)
cb = fig.colorbar(im, ax=ax, pad=0.01)
cb.set_label("gCO₂/kWh (darker = dirtier)", color=INK2)
cb.outline.set_visible(False)
cb.ax.tick_params(colors=MUTED, length=0)
img_heat = save(fig, "time_of_day_heatmap.png")
night_day = q(f"""
    select halfhour_slot, date_trunc('week', date_local) as wk, actual_gco2_kwh as y
    from marts.fct_grid_halfhourly
    where {LAST_12M} and halfhour_slot in (6, 36) and actual_gco2_kwh is not null
""")
tod_ci = block_ci(
    night_day,
    lambda s: 1 - s.y[s.halfhour_slot == 6].mean() / s.y[s.halfhour_slot == 36].mean(),
)
F["time_of_day"] = (
    f"Over the last 12 months the cleanest half-hour on average starts at **{hhmm(best.halfhour_slot)}**"
    f" ({best.g:.0f} gCO₂/kWh) and the dirtiest at **{hhmm(worst.halfhour_slot)}** ({worst.g:.0f}). "
    f"Electricity at 03:00 is **{1 - s3am / s6pm:.0%} cleaner** than at 18:00 "
    f"({s3am:.0f} vs {s6pm:.0f}; {ci(*tod_ci)})."
)

# 3. Weekday vs weekend -----------------------------------------------------------------------
wk = (
    q(f"""
    select is_weekend, avg(actual_gco2_kwh) as g from marts.fct_grid_halfhourly
    where {LAST_12M} and not is_bank_holiday group by 1
""")
    .set_index("is_weekend")
    .g
)
bh = q(f"""select avg(actual_gco2_kwh) from marts.fct_grid_halfhourly
           where {LAST_12M} and is_bank_holiday""").iloc[0, 0]  # fmt: skip
week_rows = q(f"""
    select is_weekend, date_trunc('week', date_local) as wk, actual_gco2_kwh as y
    from marts.fct_grid_halfhourly
    where {LAST_12M} and not is_bank_holiday and actual_gco2_kwh is not null
""")
wkd_ci = block_ci(week_rows, lambda s: 1 - s.y[s.is_weekend].mean() / s.y[~s.is_weekend].mean())
F["weekend"] = (
    f"Weekends average **{wk[True]:.0f}** gCO₂/kWh vs **{wk[False]:.0f}** on weekdays "
    f"(**{1 - wk[True] / wk[False]:.0%} cleaner**, {ci(*wkd_ci)}; lower demand); bank holidays "
    f"average {bh:.0f}."
)

# 4. Drivers: wind, temperature, sunshine (OLS, 2023+) ------------------------------------------
d = q("""
    select actual_gco2_kwh as y, wind_speed_100m_kmh as wind, temperature_c as temp,
           solar_radiation_wm2 as solar, date_trunc('week', date_local) as wk
    from marts.fct_grid_halfhourly
    where year >= 2023 and actual_gco2_kwh is not null and wind_speed_100m_kmh is not null
""")


def wind_effect(s) -> float:
    """gCO₂/kWh change per +10 km/h of wind, holding temperature and sunshine fixed (OLS)."""
    Xs = np.column_stack([np.ones(len(s)), s.wind, s.temp, s.solar])
    return -10 * np.linalg.lstsq(Xs, s.y, rcond=None)[0][1]


X = np.column_stack([np.ones(len(d)), d.wind, d.temp, d.solar])
beta, *_ = np.linalg.lstsq(X, d.y, rcond=None)
wind_ci = block_ci(d, wind_effect, reps=500)
r2 = 1 - ((d.y - X @ beta) ** 2).sum() / ((d.y - d.y.mean()) ** 2).sum()
r_wind = np.corrcoef(d.wind, d.y)[0, 1]
bins = d.assign(b=(d.wind // 5) * 5).groupby("b").y.agg(["mean", "size"])
bins = bins[bins["size"] >= 200]
fig, ax = chart("Windier = cleaner (2023 onwards)", "gCO₂/kWh")
ax.plot(bins.index + 2.5, bins["mean"], color=BLUE, linewidth=2, marker="o", markersize=5)
ax.set_xlabel("Average 100 m wind speed at wind-farm points (km/h)", color=INK2)
img_wind = save(fig, "wind_vs_intensity.png")
F["drivers"] = (
    f"Wind speed alone correlates at r = **{r_wind:.2f}** with intensity. A linear model on wind, "
    f"temperature and sunshine explains **R² = {r2:.2f}**: each +10 km/h of wind is associated with "
    f"**{-10 * beta[1]:.0f} gCO₂/kWh lower** intensity ({ci(*wind_ci, '.0f')}), holding the "
    f"others fixed (association, not "
    f"proof of cause: wind also co-varies with season and demand)."
)

# 5. Regional gap (last 12 months, DNO regions only) ----------------------------------------------
reg = q("""
    select d.short_name, avg(f.forecast_gco2_kwh) as g,
           avg((f.intensity_index = 'very low')::int) as very_low_share
    from marts.fct_regional_halfhourly f join marts.dim_region d using (region_id)
    where not f.is_aggregate
      and f.period_from > (select max(period_from) from marts.fct_regional_halfhourly) - interval 365 day
    group by 1 order by g
""")
fig, ax = chart("Average forecast intensity by region (last 12 months)", "", size=(8, 5))
ax.grid(axis="x", color=GRID, linewidth=0.8)
ax.grid(axis="y", visible=False)
ax.barh(reg.short_name, reg.g, color=BLUE, height=0.6)
ax.invert_yaxis()
ax.set_xlabel("gCO₂/kWh", color=INK2)
img_reg = save(fig, "regional_intensity.png")
cleanest, dirtiest = reg.iloc[0], reg.iloc[-1]
F["regions"] = (
    f"**{cleanest.short_name}** is the cleanest region (**{cleanest.g:.0f}** gCO₂/kWh on average; "
    f"'very low' {cleanest.very_low_share:.0%} of the time) and **{dirtiest.short_name}** the "
    f"dirtiest (**{dirtiest.g:.0f}**), a **{dirtiest.g / cleanest.g:.0f}×** gap within one grid."
)

# 6. How good is NESO's published forecast? -------------------------------------------------------
acc = q("""
    select year, avg(abs(forecast_gco2_kwh - actual_gco2_kwh)) as mae,
           avg(forecast_gco2_kwh - actual_gco2_kwh) as bias, avg(actual_gco2_kwh) as mean_actual
    from marts.fct_grid_halfhourly
    where forecast_gco2_kwh is not null and actual_gco2_kwh is not null and year between 2018 and 2025
    group by 1 order by 1
""")
a0, a1 = acc.iloc[0], acc.iloc[-1]
F["neso"] = (
    f"NESO's published forecast missed actuals by **{a1.mae:.1f} gCO₂/kWh** on average in "
    f"{a1.year:.0f} (MAE; {a1.mae / a1.mean_actual:.0%} of the mean), vs {a0.mae:.1f} in "
    f"{a0.year:.0f}, with a bias of {a1.bias:+.1f}. This is the benchmark the Phase 8 model is "
    f"measured against (the API does not say how far ahead each published forecast was made)."
)

# 7. Impact: when an EV charges (last 12 months) ----------------------------------------------------
night = q(f"""
    select local_ts, actual_gco2_kwh as g,
           cast(local_ts - interval 7 hour as date) as night_of  -- 18:00 day D .. 07:00 day D+1
    from marts.fct_grid_halfhourly
    where {LAST_12M} and (hour_local >= 18 or hour_local < 7)
    order by local_ts
""")
nights = []
for night_of, n in night.groupby("night_of"):
    if len(n) < 26 or n.g.isna().any():  # 13 h of half-hours, complete nights only
        continue
    window = n.g.rolling(6).mean()  # 3-hour charge (6 half-hours)
    # plug in at 18:00 vs cleanest 3 h overnight
    nights.append((pd.Timestamp(night_of).to_period("W").start_time, window.iloc[5], window.min()))
ev = pd.DataFrame(nights, columns=["wk", "plug_in", "smart"])
plug_in, smart = ev.plug_in.to_numpy(), ev.smart.to_numpy()
kwh, n_evs = 7.0, 1_000_000
saved_kg = ((plug_in - smart) * kwh).sum() / 1000 * 365 / len(nights)
ev_ci = block_ci(ev, lambda s: 1 - s.smart.mean() / s.plug_in.mean())
kg_ci = block_ci(ev, lambda s: ((s.plug_in - s.smart) * kwh).mean() * 365 / 1000)
F["ev"] = (
    f"A 7 kWh overnight EV charge started on plug-in at 18:00 averaged **{plug_in.mean():.0f}** "
    f"gCO₂/kWh; the cleanest 3-hour window before 07:00 averaged **{smart.mean():.0f}** "
    f"(**{1 - smart.mean() / plug_in.mean():.0%} less**, {ci(*ev_ci)}). That is **{saved_kg:.0f} kg "
    f"CO₂ per car per year** ({ci(*kg_ci, '.0f')} kg), or **~{saved_kg * n_evs / 1000:,.0f} tonnes** across 1 million smart-charging "
    f"EVs, at zero cost to the driver ({len(nights)} complete nights analysed)."
)

# 8. Data quality ---------------------------------------------------------------------------------
dq = q("""
    select count(*) as n, count(*) filter (where is_missing) as missing,
           count(*) filter (where not is_missing and actual_gco2_kwh is null) as no_actual
    from marts.fct_grid_halfhourly
""").iloc[0]
invalid = q("select count(*) from staging.stg_intensity_national where is_forecast_invalid").iloc[
    0, 0
]
F["quality"] = (
    f"{dq.n:,} half-hours on a complete time spine since Sept 2017: {dq.missing} "
    f"({dq.missing / dq.n:.2%}) never returned by the API, {dq.no_actual} without an actual, and "
    f"{invalid} impossible NESO forecasts nulled in staging. Figures above use complete data only."
)

# Report --------------------------------------------------------------------------------------------
report = f"""# Findings

_Generated by `analysis/findings.py` from the dbt marts; every number is computed, none typed.
Times are UK local time. "Last 12 months" = the 365 days to the latest loaded half-hour._

**How sure are these numbers?** Each headline effect has a 95% confidence interval from a block
bootstrap: whole weeks are resampled with replacement (1,000 times; 500 for the regression), so
multi-day weather spells stay together and the intervals aren't falsely narrow. An interval that
doesn't include zero means the effect is very unlikely to be chance.

## 1. The grid has decarbonised fast
{F["trend"]}

![Annual intensity]({img_trend})
![Gas vs wind]({img_mix})

## 2. Time of day matters more than anything you control
{F["time_of_day"]}

![Heatmap]({img_heat})

## 3. Weekends are cleaner
{F["weekend"]}

## 4. Wind is the main driver
{F["drivers"]}

![Wind vs intensity]({img_wind})

## 5. Where you live matters
{F["regions"]}

![Regions]({img_reg})

## 6. The official forecast is the benchmark to beat
{F["neso"]}

## 7. Impact: smart EV charging
{F["ev"]}

## Data quality
{F["quality"]}
"""
(ROOT / "docs" / "findings.md").write_text(report, encoding="utf-8")
print(report)
