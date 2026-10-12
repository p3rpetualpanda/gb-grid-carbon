"""GB Grid Carbon dashboard (Phase 9).  Run:  streamlit run app/streamlit_app.py

Reads MotherDuck when MOTHERDUCK_TOKEN is set (Streamlit secrets or env var), otherwise the local
DuckDB file. Queries dbt marts only; every timestamp is UTC in the warehouse, shown in UK time.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

LOCAL_DB = Path(__file__).resolve().parents[1] / "data" / "gridcarbon.duckdb"
TZ = "Europe/London"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"  # categorical slots 1-3
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
REPO = "https://github.com/p3rpetualpanda/gb-grid-carbon"


def greenest_window(forecast: pd.Series, hours: float) -> tuple[pd.Timestamp, float]:
    """Start and mean intensity of the cleanest contiguous `hours` block of a half-hourly series."""
    slots = max(1, round(hours * 2))
    if len(forecast) < slots:
        raise ValueError(f"need at least {slots} half-hours, got {len(forecast)}")
    means = forecast.rolling(slots).mean().dropna()
    end = forecast.index.get_loc(means.idxmin())
    return forecast.index[end - slots + 1], float(means.min())


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
        f"Data refreshes every 3 hours (last: {hhmm(refreshed)} UK time). [Source code]({REPO})"
    )

    latest_issue = q("""
        select period_from, model_gco2_kwh, neso_gco2_kwh from marts.fct_model_vs_official
        where issued_at = (select max(issued_at) from marts.fct_model_vs_official)
        order by period_from
    """)
    forecast = latest_issue.set_index("period_from")["model_gco2_kwh"]

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
        c1.metric(
            f"Latest actual ({hhmm(latest.period_from)})",
            f"{latest.actual_gco2_kwh} gCO₂/kWh",
            latest.intensity_index,
            delta_color="off",
        )
        c2.metric("Cleanest 3 h in the next 48 h starts", hhmm(start))
        c3.metric("Forecast intensity in that window", f"{mean:.0f} gCO₂/kWh")
        past = q("""
            select period_from, actual_gco2_kwh from marts.fct_grid_halfhourly
            where period_from > (select max(period_from) from marts.fct_grid_halfhourly)
                                - interval 36 hour
        """)
        fig = go.Figure()
        fig.add_scatter(x=uk(past.period_from), y=past.actual_gco2_kwh, name="Actual",
                        line=dict(color=BLUE, width=2))  # fmt: skip
        fig.add_scatter(x=uk(latest_issue.period_from), y=latest_issue.model_gco2_kwh,
                        name="Our forecast", line=dict(color=ORANGE, width=2))  # fmt: skip
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
        st.caption("Based on our latest 48 h forecast. 7 kWh of EV charging at the cleanest time "
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
        st.plotly_chart(fig, width="stretch")

    with accuracy_tab:
        scored = q("""
            select case when lead <= 12 then '0-6h' when lead <= 24 then '6-12h'
                        when lead <= 48 then '12-24h' else '24-48h' end as lead_band,
                   count(*) as n, avg(model_abs_error) as our_mae, avg(neso_abs_error) as neso_mae
            from marts.fct_model_vs_official
            where actual_gco2_kwh is not null and neso_abs_error is not null
            group by 1 order by min(lead)
        """)
        st.subheader("Live track record: our forecast vs NESO's, issued in the same run")
        if scored.empty:
            st.info("The track record builds up as the pipeline runs every 3 hours.")
        else:
            st.dataframe(scored.round(1), hide_index=True, width="stretch")
        st.caption(f"12-month backtest (our model 26.8 MAE vs 44.3 best naïve baseline): "
                   f"[docs/backtest.md]({REPO}/blob/main/docs/backtest.md)")  # fmt: skip

    with about_tab:
        st.markdown(f"""
**How it works.** A Python pipeline pulls the NESO Carbon Intensity API and Open-Meteo weather into
MotherDuck every 3 hours (GitHub Actions); dbt models and tests the data; a LightGBM model
forecasts the next 48 hours; this app reads the result.
[Code]({REPO}) · [dbt docs](https://p3rpetualpanda.github.io/gb-grid-carbon/)

**Data:** Carbon Intensity API, National Energy System Operator (CC BY 4.0); weather by
Open-Meteo.com (CC BY 4.0). Regional figures are forecasts (NESO publishes no regional actuals).
""")


if __name__ == "__main__":  # Streamlit runs the script as __main__; tests import the helpers only
    main()
