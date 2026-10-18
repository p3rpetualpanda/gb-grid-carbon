"""GB Grid Carbon dashboard (Phase 9).  Run:  streamlit run app/streamlit_app.py

Reads MotherDuck when MOTHERDUCK_TOKEN is set (Streamlit secrets or env var), otherwise the local
DuckDB file. Queries dbt marts only; every timestamp is UTC in the warehouse, shown in UK time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pandas as pd

LOCAL_DB = Path(__file__).resolve().parents[1] / "data" / "gridcarbon.duckdb"
TZ = "Europe/London"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET = (  # categorical slots 1-6, in order
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#7c5cd6"
)  # fmt: skip
FUEL_COLOURS = {"Wind": BLUE, "Gas": ORANGE, "Nuclear": AQUA, "Solar": YELLOW,
                "Imports": MAGENTA, "Other": VIOLET}  # fmt: skip
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
REPO = "https://github.com/p3rpetualpanda/gb-grid-carbon"
BACKTEST_RESULTS = Path(__file__).resolve().parents[1] / "docs" / "backtest_results.json"


def backtest_caption(path: Path = BACKTEST_RESULTS) -> str:
    """One-line backtest summary from the file analysis/backtest.py writes."""
    link = f"[docs/backtest.md]({REPO}/blob/main/docs/backtest.md)"
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"12-month backtest: {link}"
    return (f"12-month backtest, run {r['run_date']} (our model {r['model_mae']:.1f} MAE vs "
            f"{r['best_baseline_mae']:.1f} best naïve baseline): {link}")  # fmt: skip


def greenest_window(forecast: pd.Series, hours: float) -> tuple[pd.Timestamp, float]:
    """Start and mean intensity of the cleanest contiguous `hours` block of a half-hourly series."""
    slots = max(1, round(hours * 2))
    if len(forecast) < slots:
        raise ValueError(f"need at least {slots} half-hours, got {len(forecast)}")
    means = forecast.rolling(slots).mean().dropna()
    end = forecast.index.get_loc(means.idxmin())
    return forecast.index[end - slots + 1], float(means.min())


NESO_LEADS = 12  # NESO beats our model at 0-6 h (live and backtest), so trust it there


def _shows_neso(issue: pd.DataFrame) -> pd.Series:
    return (issue["lead"] <= NESO_LEADS) & issue["neso_gco2_kwh"].notna()


def best_forecast(issue: pd.DataFrame) -> pd.Series:
    """Per half-hour: NESO's forecast for the first 6 h (when it has one), ours after that."""
    blended = issue["neso_gco2_kwh"].where(_shows_neso(issue), issue["model_gco2_kwh"])
    return blended.set_axis(issue["period_from"]).astype(float)


def typical_error(issue: pd.DataFrame, scored: pd.DataFrame) -> pd.Series:
    """Per half-hour: the live MAE, in that lead band, of whichever forecast best_forecast shows."""
    band = pd.cut(issue["lead"], [0, 12, 24, 48, 96],
                  labels=["0-6 h", "6-12 h", "12-24 h", "24-48 h"]).astype(str)  # fmt: skip
    mae = scored.set_index("band")
    err = band.map(mae["neso_mae"]).where(_shows_neso(issue), band.map(mae["our_mae"]))
    return err.set_axis(issue["period_from"]).astype(float)


def main() -> None:
    import plotly.express as px
    import plotly.graph_objects as go
    import streamlit as st

    st.set_page_config(page_title="GB Grid Carbon", page_icon="⚡", layout="wide")

    @st.cache_resource
    def connection():
        try:
            token = st.secrets["MOTHERDUCK_TOKEN"]
        except Exception:  # no secrets file locally
            token = os.environ.get("MOTHERDUCK_TOKEN")
        if token:
            con = duckdb.connect("md:gridcarbon", config={"motherduck_token": token})
        elif LOCAL_DB.exists():
            con = duckdb.connect(str(LOCAL_DB), read_only=True)
        else:
            st.error("No data: set the `MOTHERDUCK_TOKEN` secret (Manage app → Settings → Secrets)")
            st.stop()
        con.execute("SET TimeZone = 'UTC'")
        return con

    @st.cache_data(ttl=900)
    def q(sql: str) -> pd.DataFrame:
        return connection().cursor().sql(sql).df()  # cursor: DuckDB connections aren't thread-safe

    def uk(ts) -> pd.Series | pd.Timestamp:
        return ts.dt.tz_convert(TZ) if isinstance(ts, pd.Series) else ts.tz_convert(TZ)

    def hhmm(ts: pd.Timestamp) -> str:
        return uk(ts).strftime("%a %H:%M")

    st.title("GB Grid Carbon Tracker")
    refreshed = q(
        "select max(finished_at) as t from meta.pipeline_runs where status = 'success'"
    ).t.iloc[0]
    st.caption(
        f"How clean is Great Britain's electricity, and when will it be cleanest? "
        f"Data refreshes every few hours (last: {hhmm(refreshed)} UK time). [Source code]({REPO})"
    )

    latest_issue = q("""
        select period_from, lead, model_gco2_kwh, neso_gco2_kwh from marts.fct_model_vs_official
        where issued_at = (select max(issued_at) from marts.fct_model_vs_official)
        order by period_from
    """)
    forecast = best_forecast(latest_issue)
    scored = q("""
        select coalesce(band, 'All') as band,
               count(distinct issued_at) as issues, count(*) as n,
               min(period_from) as first_target, max(period_from) as last_target,
               avg(model_abs_error) as our_mae, avg(neso_abs_error) as neso_mae,
               avg(model_abs_error) - avg(neso_abs_error) as gap,
               avg(model_gco2_kwh - actual_gco2_kwh) as our_bias,
               avg(neso_gco2_kwh - actual_gco2_kwh) as neso_bias,
               avg((model_abs_error < neso_abs_error)::int) * 100 as we_closer
        from (
            select *, case when lead <= 12 then '0-6 h' when lead <= 24 then '6-12 h'
                           when lead <= 48 then '12-24 h' else '24-48 h' end as band
            from marts.fct_model_vs_official
            where actual_gco2_kwh is not null and neso_abs_error is not null
        )
        group by grouping sets ((band), ())
        order by grouping(band), min(lead)
    """)
    err = typical_error(latest_issue, scored)

    now_tab, planner_tab, greenest_tab, trends_tab, regions_tab, accuracy_tab, about_tab = st.tabs(
        [
            "Now",
            "Planner",
            "When is it greenest?",
            "Trends",
            "Regions",
            "Forecast accuracy",
            "About",
        ]
    )

    with now_tab:
        latest = q("""
            select period_from, actual_gco2_kwh, intensity_index from marts.fct_grid_halfhourly
            where actual_gco2_kwh is not null order by period_from desc limit 1
        """).iloc[0]
        start, mean = greenest_window(forecast, 3)
        c1, c2, c3 = st.columns(3)
        # Index goes in the label: metric's delta slot always draws an up/down arrow.
        c1.metric(
            f"Latest actual ({hhmm(latest.period_from)}) · {latest.intensity_index}",
            f"{latest.actual_gco2_kwh} gCO₂/kWh",
        )
        c2.metric("Cleanest 3 h in the next 48 h starts", hhmm(start))
        c3.metric("Forecast intensity in that window", f"{mean:.0f} gCO₂/kWh")
        past = q("""
            select period_from, actual_gco2_kwh from marts.fct_grid_halfhourly
            where period_from > (select max(period_from) from marts.fct_grid_halfhourly)
                                - interval 36 hour
            order by period_from
        """)
        fig = go.Figure()
        if err.notna().any():
            # Band first so the lines draw on top; tonexty fills between lower and upper.
            fig.add_scatter(x=uk(forecast.index), y=(forecast - err).clip(lower=0),
                            line=dict(width=0), hoverinfo="skip", showlegend=False)  # fmt: skip
            fig.add_scatter(x=uk(forecast.index), y=forecast + err, line=dict(width=0),
                            fill="tonexty", fillcolor="rgba(235, 104, 52, 0.15)",
                            hoverinfo="skip", name="± typical error (live MAE)")  # fmt: skip
        fig.add_scatter(x=uk(past.period_from), y=past.actual_gco2_kwh, name="Actual",
                        line=dict(color=BLUE, width=2))  # fmt: skip
        fig.add_scatter(x=uk(forecast.index), y=forecast, name="Our forecast (NESO's for 0-6 h)",
                        line=dict(color=ORANGE, width=2))  # fmt: skip
        fig.add_scatter(
            x=uk(latest_issue.period_from),
            y=latest_issue.neso_gco2_kwh,
            name="NESO forecast",
            line=dict(color=AQUA, width=2, dash="dot"),
        )
        fig.update_layout(
            title="Carbon intensity: last 36 h and next 48 h (UK time)",
            yaxis_title="gCO₂/kWh",
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, width="stretch")
        if err.notna().any():
            st.caption("Shaded band: the forecast ± its average live error at that lead time "
                       "(Forecast accuracy tab).")  # fmt: skip

        outlook = q(f"""
            select period_from, wind_mw, embedded_wind_mw, embedded_solar_mw
            from marts.fct_wind_solar_outlook
            where period_from between '{forecast.index.min()}' and '{forecast.index.max()}'
            order by period_from
        """)
        st.subheader("Wind and solar outlook (NESO forecast)")
        if outlook.empty:
            st.info("No NESO wind or solar forecast loaded for the next 48 h yet.")
        else:
            fig = go.Figure()
            for col, name, dash, colour in (
                ("wind_mw", "Wind farms (transmission)", "solid", BLUE),
                ("embedded_wind_mw", "Wind on local networks", "dot", BLUE),
                ("embedded_solar_mw", "Solar", "solid", YELLOW),
            ):
                fig.add_scatter(x=uk(outlook.period_from), y=outlook[col] / 1000, name=name,
                                line=dict(color=colour, width=2, dash=dash))  # fmt: skip
            fig.update_layout(
                title="Forecast wind and solar output, next 48 h (UK time)",
                yaxis_title="GW",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig, width="stretch")
            st.caption("More wind and sun means lower carbon. NESO publishes its wind farm "
                       "forecast once a day for that day only, so that line stops early; the "
                       "local-network wind and solar forecast runs 3 days ahead.")  # fmt: skip

        mix = q("""
            select period_from, wind_pct as "Wind", gas_pct as "Gas", nuclear_pct as "Nuclear",
                   solar_pct as "Solar", imports_pct as "Imports",
                   coalesce(biomass_pct, 0) + coalesce(hydro_pct, 0) + coalesce(coal_pct, 0)
                       + coalesce(other_pct, 0) as "Other"
            from marts.fct_grid_halfhourly
            where gas_pct is not null and period_from > (
                select max(period_from) from marts.fct_grid_halfhourly where gas_pct is not null
            ) - interval 7 day
            order by period_from
        """)
        st.subheader("What's powering the grid")
        if mix.empty:
            st.info("No generation mix loaded yet.")
        else:
            now_mix = mix.iloc[-1]
            shares = now_mix[list(FUEL_COLOURS)].astype(float).sort_values()
            c1, c2 = st.columns([1, 2])
            fig = px.bar(x=shares.values, y=shares.index, orientation="h", text_auto=".0f",
                         color=shares.index, color_discrete_map=FUEL_COLOURS,
                         labels=dict(x="% of generation", y=""))  # fmt: skip
            fig.update_layout(title=f"Now ({hhmm(now_mix.period_from)})", showlegend=False)
            fig.update_xaxes(range=[0, shares.max() * 1.2])  # room for the outside labels
            fig.update_traces(textposition="outside", cliponaxis=False,
                              hovertemplate="%{y}: %{x:.1f}%<extra></extra>")  # fmt: skip
            c1.plotly_chart(fig, width="stretch")
            long = mix.melt("period_from", var_name="fuel", value_name="pct")
            long["period_from"] = uk(long["period_from"])
            fig = px.area(long, x="period_from", y="pct", color="fuel",
                          color_discrete_map=FUEL_COLOURS,
                          category_orders=dict(fuel=list(FUEL_COLOURS)),
                          labels=dict(period_from="", pct="% of generation", fuel=""))  # fmt: skip
            fig.update_layout(title="Last 7 days", hovermode="x unified")
            c2.plotly_chart(fig, width="stretch")
            st.caption("Other = biomass, hydro, coal and other sources. Shares of GB generation "
                       "(Carbon Intensity API).")  # fmt: skip

    with planner_tab:
        st.subheader("When should I run it?")
        hours = st.slider("How long does it run? (hours)", 0.5, 8.0, 3.0, 0.5)
        start, mean = greenest_window(forecast, hours)
        now_mean = forecast.iloc[: max(1, round(hours * 2))].mean()
        st.success(
            f"Start at **{hhmm(start)}** for the cleanest {hours:g} h in the next 48 h: "
            f"about **{mean:.0f} gCO₂/kWh**, vs {now_mean:.0f} if you start now "
            f"(**{1 - mean / now_mean:.0%} less CO₂**)."
        )
        st.caption("Based on our latest 48 h forecast (NESO's for the first 6 h). "
                   "7 kWh of EV charging at the cleanest time "
                   f"emits about {7 * mean / 1000:.1f} kg CO₂.")  # fmt: skip

    with greenest_tab:
        heat = q("""
            select month, hour_local, avg(actual_gco2_kwh) as g from marts.fct_grid_halfhourly
            where period_from > (select max(period_from) from marts.fct_grid_halfhourly)
                                - interval 365 day
            group by all
        """).pivot(index="month", columns="hour_local", values="g")
        fig = px.imshow(heat, color_continuous_scale=SEQ_BLUE, aspect="auto",
                        labels=dict(x="UK hour", y="Month", color="gCO₂/kWh"))  # fmt: skip
        fig.update_layout(
            title="Average intensity by month and hour, last 12 months (darker = dirtier)"
        )
        st.plotly_chart(fig, width="stretch")
        st.caption("Midday is now the cleanest time from spring to autumn (solar); evenings are "
                   "dirtiest all year. Full analysis: docs/findings.md in the repo.")  # fmt: skip

    with trends_tab:
        daily = q("select date_local, mean_actual_gco2_kwh as g from marts.agg_daily order by 1")
        daily["avg30"] = daily.g.rolling(30, min_periods=15).mean()
        fig = go.Figure()
        fig.add_scatter(x=daily.date_local, y=daily.avg30, name="30-day average",
                        line=dict(color=BLUE, width=2))  # fmt: skip
        fig.update_layout(title="GB carbon intensity since 2017 (30-day rolling average)",
                          yaxis_title="gCO₂/kWh", hovermode="x unified")  # fmt: skip
        st.plotly_chart(fig, width="stretch")

    with regions_tab:
        reg = q("""
            select d.short_name as region, avg(f.forecast_gco2_kwh) as g
            from marts.fct_regional_halfhourly f join marts.dim_region d using (region_id)
            where not f.is_aggregate and f.period_from >
                  (select max(period_from) from marts.fct_regional_halfhourly) - interval 30 day
            group by 1 order by g
        """)
        fig = px.bar(reg, x="g", y="region", orientation="h", color_discrete_sequence=[BLUE],
                     text_auto=".0f", labels=dict(g="gCO₂/kWh", region=""))  # fmt: skip
        fig.update_layout(title="Average forecast intensity by region, last 30 days",
                          yaxis=dict(autorange="reversed"))  # fmt: skip
        # outside: plotly hides labels on zero-width bars (North Scotland is ~0)
        fig.update_traces(textposition="outside", textangle=0, cliponaxis=False)
        st.plotly_chart(fig, width="stretch")

    with accuracy_tab:
        st.subheader("Live track record: our forecast vs NESO's, issued in the same run")
        if scored.empty:
            st.info("The track record builds up as the pipeline runs every few hours.")
        else:
            scored["targets"] = (uk(scored.first_target).dt.strftime("%d %b")
                                 + " – " + uk(scored.last_target).dt.strftime("%d %b"))  # fmt: skip
            g = st.column_config.NumberColumn
            st.dataframe(
                scored.drop(columns=["first_target", "last_target"]),
                hide_index=True,
                column_order=[
                    "band",
                    "issues",
                    "n",
                    "targets",
                    "our_mae",
                    "neso_mae",
                    "gap",
                    "we_closer",
                    "our_bias",
                    "neso_bias",
                ],
                column_config={
                    "band": "Lead time",
                    "issues": g("Forecasts", help="Pipeline runs scored in this band"),
                    "n": g("Half-hours", help="Half-hours with both an actual and NESO forecast"),
                    "targets": "Target dates",
                    "our_mae": g("Our MAE", format="%.1f", help="Mean absolute error, gCO₂/kWh"),
                    "neso_mae": g("NESO MAE", format="%.1f", help="Mean absolute error, gCO₂/kWh"),
                    "gap": g("Ours − NESO", format="%+.1f", help="Negative = we were better"),
                    "we_closer": g(
                        "We were closer",
                        format="%.0f%%",
                        help="Share of half-hours where our forecast beat NESO's",
                    ),
                    "our_bias": g(
                        "Our bias", format="%+.1f", help="Mean forecast − actual: + = over-forecast"
                    ),
                    "neso_bias": g(
                        "NESO bias",
                        format="%+.1f",
                        help="Mean forecast − actual: + = over-forecast",
                    ),
                },
            )
            first, last = uk(scored.first_target.min()), uk(scored.last_target.max())
            days = (last.date() - first.date()).days + 1  # calendar dates spanned, UK time
            note = (f" Only {days} days of live data so far: each lead band covers different "
                    "target dates, so compare bands with care." if days < 28 else "")  # fmt: skip
            st.caption(f"Errors in gCO₂/kWh.{note}")
        st.caption(backtest_caption())

    with about_tab:
        st.markdown(f"""
**How it works.** A Python pipeline pulls the NESO Carbon Intensity API and Open-Meteo weather into
MotherDuck every few hours (GitHub Actions); dbt models and tests the data; a LightGBM model
forecasts the next 48 hours; this app reads the result.
[Code]({REPO}) · [dbt docs](https://p3rpetualpanda.github.io/gb-grid-carbon/)

**Data:** Carbon Intensity API, National Energy System Operator (CC BY 4.0); weather by
Open-Meteo.com (CC BY 4.0). Regional figures are forecasts (NESO publishes no regional actuals).
""")


if __name__ == "__main__":  # Streamlit runs the script as __main__; tests import the helpers only
    main()
