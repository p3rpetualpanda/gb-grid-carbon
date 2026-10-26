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


def day_windows(day: pd.Series, hours: float) -> pd.Series:
    """Mean of every `hours` window of a typical day (48 half-hour slots), keyed by start slot.
    Windows wrap past midnight, so a 23:00-02:00 block counts."""
    slots = max(1, round(hours * 2))
    twice = pd.concat([day, day], ignore_index=True)
    return twice.rolling(slots).mean().shift(-(slots - 1)).iloc[:48].set_axis(day.index)


def typical_day(typical: pd.DataFrame) -> pd.DataFrame:
    """Mean intensity by month x half-hour slot from weekday/weekend rows, weighted by their
    half-hour counts `n` (a plain mean would count weekends as half of all days). Months
    missing any slot are dropped."""
    sums = typical.assign(gn=typical["g"] * typical["n"]).groupby(["month", "halfhour_slot"])
    sums = sums[["gn", "n"]].sum()
    return (sums["gn"] / sums["n"]).unstack().reindex(columns=range(48)).dropna()


MAX_TASK_H = 8  # the longest task length offered


def upcoming(
    forecast: pd.Series, now: pd.Timestamp, min_hours: float = MAX_TASK_H
) -> tuple[pd.Series, float]:
    """The forecast from the current half-hour on, and how many hours that is: the latest issue
    can be hours old, and a window that already started is no advice. If under `min_hours`
    remain (the pipeline is far behind), the whole forecast comes back so the page still renders,
    with the hours that are still ahead."""
    left = forecast[forecast.index >= now.floor("30min")]
    return (left if len(left) >= 2 * min_hours else forecast), len(left) / 2


def hhmm(ts: pd.Timestamp) -> str:
    """Weekday and UK time, or a dash when there is no timestamp (an empty warehouse)."""
    return "–" if pd.isna(ts) else ts.tz_convert(TZ).strftime("%a %H:%M")


def motherduck_url() -> str:
    """Same database as the pipeline (Settings.motherduck_database)."""
    return f"md:{os.environ.get('MOTHERDUCK_DATABASE', 'gridcarbon')}"


# The latest issue whose every half-hour has an actual: a fully played-out example.
EXAMPLE_SQL = """
    select issued_at, period_from, model_gco2_kwh, neso_gco2_kwh, actual_gco2_kwh
    from marts.fct_model_vs_official
    where issued_at = (
        select issued_at from marts.fct_model_vs_official
        group by issued_at having count(actual_gco2_kwh) = count(*)
        order by issued_at desc limit 1
    )
    order by period_from
"""


def slot_label(slot: int) -> str:
    return f"{slot // 2 % 24:02d}:{slot % 2 * 30:02d}"


PLAIN_BANDS = {"0-6 h": "Next 6 hours", "6-12 h": "6–12 hours ahead",
               "12-24 h": "12–24 hours ahead", "24-48 h": "1–2 days ahead",
               "All": "All (next 48 hours)"}  # fmt: skip
NESO_LEADS = 6  # live v4: NESO beats us for the first 3 h, we beat it at 3-6 h (26 Oct)


def rows_or_empty(run, sql: str) -> pd.DataFrame:
    """Run `sql`, or return no rows if a table it reads doesn't exist yet (a new dbt model goes
    live with the code, but MotherDuck only has it after the next pipeline run)."""
    try:
        return run(sql)
    except duckdb.CatalogException:
        return pd.DataFrame()


def _shows_neso(issue: pd.DataFrame) -> pd.Series:
    return (issue["lead"] <= NESO_LEADS) & issue["neso_gco2_kwh"].notna()


def best_forecast(issue: pd.DataFrame) -> pd.Series:
    """Per half-hour: NESO's forecast for the first 3 h (when it has one), ours after that."""
    blended = issue["neso_gco2_kwh"].where(_shows_neso(issue), issue["model_gco2_kwh"])
    return blended.set_axis(issue["period_from"]).astype(float)


MIN_SCORED = 100  # scored half-hours a model version needs in a band before its own MAE is used


BAND_EDGES = [0, 12, 24, 48, 96]  # lead in half-hours
BAND_LABELS = ["0-6 h", "6-12 h", "12-24 h", "24-48 h"]
# The same bands in SQL, for the accuracy queries.
BAND_SQL = (
    "case "
    + " ".join(
        f"when lead <= {hi} then '{band}'"
        for hi, band in zip(BAND_EDGES[1:], BAND_LABELS, strict=True)
    )
    + " end"
)


def _bands(issue: pd.DataFrame) -> pd.Series:
    return pd.cut(issue["lead"], BAND_EDGES, labels=BAND_LABELS).astype(str)


def typical_error(
    issue: pd.DataFrame,
    scored: pd.DataFrame,
    current: pd.DataFrame | None = None,
    min_n: int = MIN_SCORED,
) -> pd.Series:
    """Per half-hour: the live MAE, in that lead band, of whichever forecast best_forecast shows.
    For our forecast, use `current` (the MAE of the model version that issued it, with row
    counts `n`) in bands where it has at least `min_n` rows; elsewhere every version's MAE."""
    band = _bands(issue)
    mae = scored.set_index("band")
    ours = mae["our_mae"]
    if current is not None and not current.empty:
        cur = current.set_index("band")
        ours = cur["our_mae"].where(cur["n"] >= min_n).combine_first(ours)
    err = band.map(mae["neso_mae"]).where(_shows_neso(issue), band.map(ours))
    return err.set_axis(issue["period_from"]).astype(float)


def past_forecast(
    issues: pd.DataFrame,
    scored: pd.DataFrame,
    value: str = "model_gco2_kwh",
    mae: str = "our_mae",
) -> pd.Series:
    """Per past half-hour: the `value` forecast from the lead band with the lowest live `mae` that
    has one, newest issue first within it. Ranked by band, never by distance to the actual."""
    issues = issues.dropna(subset=[value])
    rank = _bands(issues).map(scored.set_index("band")[mae])
    best = issues.assign(rank=rank).sort_values(["rank", "lead"]).drop_duplicates("period_from")
    return best.set_index("period_from")[value].sort_index().astype(float)


def mixes_versions(issue: pd.DataFrame, current: pd.DataFrame, min_n: int = MIN_SCORED) -> bool:
    """True if any band where our forecast is shown falls back to every version's MAE."""
    enough = set(current.loc[current["n"] >= min_n, "band"]) if not current.empty else set()
    return not set(_bands(issue)[~_shows_neso(issue)]) <= enough


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
            con = duckdb.connect(motherduck_url(), config={"motherduck_token": token})
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
    forecast = best_forecast(latest_issue)  # the whole issue, for the chart
    advice, ahead = upcoming(forecast, pd.Timestamp.now("UTC"))  # windows from now on
    span = f"next {ahead:.0f} h" if ahead >= MAX_TASK_H else "latest forecast"
    if not forecast.empty and ahead < 36:  # the issue is over 12 h old
        passed = ", so some times below have passed" if ahead < MAX_TASK_H else ""
        st.warning(f"The pipeline is behind: the latest forecast was made "
                   f"{hhmm(forecast.index.min())} and has {ahead:g} h left{passed}.")  # fmt: skip
    scored = q(f"""
        select coalesce(band, 'All') as band,
               count(distinct issued_at) as issues, count(*) as n,
               min(period_from) as first_target, max(period_from) as last_target,
               avg(model_abs_error) as our_mae, avg(neso_abs_error) as neso_mae,
               avg(actual_gco2_kwh) as mean_actual,
               avg(model_abs_error) - avg(neso_abs_error) as gap,
               avg(model_gco2_kwh - actual_gco2_kwh) as our_bias,
               avg(neso_gco2_kwh - actual_gco2_kwh) as neso_bias,
               avg((model_abs_error < neso_abs_error)::int) * 100 as we_closer
        from (
            select *, {BAND_SQL} as band
            from marts.fct_model_vs_official
            where actual_gco2_kwh is not null and neso_abs_error is not null
        )
        group by grouping sets ((band), ())
        order by grouping(band), min(lead)
    """)
    # MAE of the model version that issued the forecast shown.
    current = q(f"""
        select band, count(*) as n, avg(model_abs_error) as our_mae
        from (
            select model_abs_error, {BAND_SQL} as band
            from marts.fct_model_vs_official
            where actual_gco2_kwh is not null and neso_abs_error is not null
              and model_version = (  -- the version of the issue the chart shows
                  select any_value(model_version) from marts.fct_model_vs_official
                  where issued_at = (select max(issued_at) from marts.fct_model_vs_official)
              )
        )
        group by band
    """)
    err = typical_error(latest_issue, scored, current)

    now_tab, greenest_tab, trends_tab, regions_tab, accuracy_tab, about_tab = st.tabs(
        [
            "Now",
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
        c1, c2, c3 = st.columns(3)
        # Index goes in the label: metric's delta slot always draws an up/down arrow.
        c1.metric(
            f"Latest actual ({hhmm(latest.period_from)}) · {latest.intensity_index}",
            f"{latest.actual_gco2_kwh} gCO₂/kWh",
        )
        if forecast.empty:  # st.stop() here would also blank every later tab
            c2.info("No forecast yet: it appears after the next pipeline run.")
        else:
            start, mean = greenest_window(advice, 3)
            c2.metric(f"Cleanest 3 h in the {span} starts", hhmm(start))
            c3.metric("Forecast intensity in that window", f"{mean:.0f} gCO₂/kWh")
            past = q("""
                select period_from, actual_gco2_kwh from marts.fct_grid_halfhourly
                where period_from > (select max(period_from) from marts.fct_grid_halfhourly)
                                    - interval 36 hour
                order by period_from
            """)
            past_issues = q(f"""
                select period_from, lead, model_gco2_kwh, neso_gco2_kwh
                from marts.fct_model_vs_official
                where period_from between '{past.period_from.min()}'
                                      and '{past.period_from.max()}'
            """)
            hindcast = past_forecast(past_issues, scored)
            neso_hindcast = past_forecast(past_issues, scored, "neso_gco2_kwh", "neso_mae")
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
            fig.add_scatter(x=uk(hindcast.index), y=hindcast,
                            name="Our past forecast",
                            line=dict(color=ORANGE, width=2, dash="dash"))  # fmt: skip
            fig.add_scatter(x=uk(neso_hindcast.index), y=neso_hindcast,
                            name="NESO past forecast",
                            line=dict(color=AQUA, width=2, dash="dash"))  # fmt: skip
            fig.add_scatter(x=uk(forecast.index), y=forecast,
                            name="Our forecast (NESO's for 0-3 h)",
                            line=dict(color=ORANGE, width=2))  # fmt: skip
            fig.add_scatter(
                x=uk(latest_issue.period_from),
                y=latest_issue.neso_gco2_kwh,
                name="NESO forecast",
                line=dict(color=AQUA, width=2, dash="dot"),
            )
            fig.update_layout(
                title="Carbon intensity: actual vs our and NESO's forecasts for the last 36 h, "
                "and the next 48 h (UK time)",
                yaxis_title="gCO₂/kWh",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig, width="stretch")
            bands = scored[scored.band != "All"].set_index("band")
            if bands.empty:
                source = "the newest forecast made before each half-hour (no live track record yet)"
            else:
                source = (f"the lead time with the lowest live error: {bands.our_mae.idxmin()} "
                          f"ahead for ours, {bands.neso_mae.idxmin()} for NESO's. Where that "
                          "lead has none, the next best is used")  # fmt: skip
            st.caption(f"Dashed lines: our (orange) and NESO's (green) forecasts for the last "
                       f"36 h, each from {source}. The lead is chosen by its average error, not "
                       "by how close it came to the actual.")  # fmt: skip
            if err.notna().any():
                mixed = ""
                if mixes_versions(latest_issue, current):
                    mixed = (" Where this model version has under 100 scored half-hours at a lead "
                             "time, earlier versions' errors are included.")  # fmt: skip
                st.caption("Shaded band: the forecast ± its average live error at that lead time "
                           f"(Forecast accuracy tab).{mixed}")  # fmt: skip

            outlook = rows_or_empty(
                q,
                f"""
                select period_from, wind_mw, embedded_wind_mw, embedded_solar_mw
                from marts.fct_wind_solar_outlook
                where period_from between '{forecast.index.min()}' and '{forecast.index.max()}'
                order by period_from
            """,
            )
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
                st.caption("More wind and sun means lower carbon. Each line is NESO's newest "
                           "forecast for that half-hour.")  # fmt: skip

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

    with greenest_tab:
        typical = q("""
            select month, halfhour_slot, is_weekend, avg(actual_gco2_kwh) as g,
                   count(actual_gco2_kwh) as n
            from marts.fct_grid_halfhourly
            where period_from > (select max(period_from) from marts.fct_grid_halfhourly)
                                - interval 365 day
            group by all
        """)
        months = list(pd.date_range("2024-01-01", periods=12, freq="MS").strftime("%B"))
        # Only months with every half-hour slot (a short history or a gap leaves some out).
        by_slot = typical_day(typical)
        if by_slot.empty:  # st.stop() here would also blank every later tab
            st.info("Not enough history yet to show typical days.")
        else:
            options = [months[i - 1] for i in by_slot.index]
            this_month = months[pd.Timestamp.now(TZ).month - 1]
            c1, c2 = st.columns(2)
            month = c1.selectbox(
                "Month",
                options,
                index=options.index(this_month) if this_month in options else len(options) - 1,
            )
            task_h = c2.select_slider(
                "How long does your task run?",
                [0.5, 1, 2, 3, 4, 6, 8],
                value=3,
                format_func=lambda h: f"{h:g} h",
            )
            m = months.index(month) + 1
            if not forecast.empty:
                start, mean = greenest_window(advice, task_h)
                now_mean = advice.iloc[: max(1, round(task_h * 2))].mean()
                st.success(
                    f"From the live forecast: start at **{hhmm(start)}** for the cleanest "
                    f"{task_h:g} h in the {span}: about **{mean:.0f} gCO₂/kWh**, "
                    f"vs {now_mean:.0f} if you start now (**{1 - mean / now_mean:.0%} less CO₂**)."
                )
                st.caption("Based on our latest 48 h forecast (NESO's for the first 3 h). "
                           "7 kWh of EV charging at the cleanest time "
                           f"emits about {7 * mean / 1000:.1f} kg CO₂.")  # fmt: skip

            def window(day: pd.Series, best: bool) -> tuple[str, float]:
                means = day_windows(day, task_h)
                start = means.idxmin() if best else means.idxmax()
                return f"{slot_label(start)}–{slot_label(start + round(task_h * 2))}", means[start]

            day = by_slot.loc[m]
            (green_t, green_g), (dirty_t, dirty_g) = window(day, True), window(day, False)
            c1, c2, c3 = st.columns(3)
            c1.metric(f"Greenest {task_h:g} h in {month}", green_t, help="UK time, last 12 months")
            c1.caption(f"Typically {green_g:.0f} gCO₂/kWh")
            c2.metric(f"Dirtiest {task_h:g} h in {month}", dirty_t)
            c2.caption(f"Typically {dirty_g:.0f} gCO₂/kWh")
            c3.metric("Emissions saved by moving it", f"{1 - green_g / dirty_g:.0%}")
            c3.caption("Greenest instead of dirtiest window, same electricity use")

            # A typical day in the chosen month: weekdays vs weekends, greenest window shaded.
            sel = typical[typical.month == m].sort_values("halfhour_slot")
            kinds = {False: "Weekdays", True: "Weekends"}
            sel = sel.assign(time=sel.halfhour_slot.map(slot_label), kind=sel.is_weekend.map(kinds))
            fig = px.line(
                sel,
                x="time",
                y="g",
                color="kind",
                color_discrete_sequence=[BLUE, ORANGE],
                category_orders=dict(kind=list(kinds.values())),
                labels=dict(time="UK time", g="gCO₂/kWh", kind=""),
            )
            fig.update_traces(line_width=2, hovertemplate="%{x}: %{y:.0f} gCO₂/kWh")
            start = day_windows(day, task_h).idxmin()
            end = start + round(task_h * 2) - 1
            for x0, x1 in [(start, end)] if end < 48 else [(start, 47), (0, end - 48)]:
                fig.add_vrect(x0=x0 - 0.5, x1=x1 + 0.5, fillcolor=AQUA, opacity=0.15, line_width=0)
            fig.add_annotation(
                x=start,
                y=1,
                yref="paper",
                text="Greenest window",
                showarrow=False,
                xanchor="left",
                font=dict(color=AQUA),
            )
            fig.update_layout(
                title=f"A typical day in {month} (average, last 12 months)",
                hovermode="x unified",
                xaxis=dict(nticks=13),
                legend=dict(orientation="h", y=1.12, x=1, xanchor="right"),
            )
            st.plotly_chart(fig, width="stretch")

            # Every month at a glance; hover for values. Darker = dirtier.
            heat = by_slot.set_axis([months[i - 1][:3] for i in by_slot.index])
            heat.columns = [slot_label(c) for c in heat.columns]
            fig = px.imshow(
                heat,
                color_continuous_scale=SEQ_BLUE,
                aspect="auto",
                labels=dict(x="UK time", y="", color="gCO₂/kWh"),
            )
            fig.update_traces(
                xgap=1, ygap=2, hovertemplate="%{y} %{x}: %{z:.0f} gCO₂/kWh<extra></extra>"
            )
            fig.update_layout(
                title="Every month: average intensity by time of day (darker = dirtier)",
                xaxis=dict(nticks=13),
            )
            st.plotly_chart(fig, width="stretch")

            rows = []
            for mo, d in by_slot.iterrows():
                (gt, gg), (dt, dg) = window(d, True), window(d, False)
                rows.append(
                    {
                        "Month": months[mo - 1],
                        "Greenest": gt,
                        "gCO₂/kWh": round(gg),
                        "Dirtiest": dt,
                        "gCO₂/kWh ": round(dg),
                        "Saving": f"{1 - gg / dg:.0%}",
                    }
                )
            st.markdown(f"**Greenest and dirtiest {task_h:g} h window, by month**")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=35 * 13 + 3)
            st.caption(
                "Averages of actual intensity over the last 12 months, UK time. Midday is "
                "cleanest from spring to autumn (solar); early evening is dirtiest all year. "
                "The green box at the top uses the live 48 h forecast instead."
            )

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
        st.subheader("How good is the forecast?")
        st.markdown(
            "Every few hours we forecast the next 48 hours. Once the real figures are published, "
            "we check how far off we were. NESO, which runs the grid, publishes its own forecast "
            "at the same time, so we score both the same way."
        )
        if scored.empty:
            st.info("The track record builds up as the pipeline runs every few hours.")
        else:
            overall = scored.set_index("band").loc["All"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Our forecast is typically off by", f"{overall.our_mae:.0f} gCO₂/kWh",
                      help="Average distance between forecast and actual, high or low")  # fmt: skip
            c2.metric("NESO's forecast is typically off by", f"{overall.neso_mae:.0f} gCO₂/kWh")
            c3.metric("Half-hours where ours was closer", f"{overall.we_closer:.0f}%")
            st.markdown(
                f"Put simply: when we forecast, say, 150 gCO₂/kWh, the real figure usually lands "
                f"within about **{overall.our_mae:.0f}** of that, which is roughly "
                f"**{overall.our_mae / overall.mean_actual:.0%}** of a typical value. The chart "
                "below splits this by how far ahead the forecast was looking."
            )

            bands = scored[scored.band != "All"].assign(label=lambda d: d.band.map(PLAIN_BANDS))
            fig = go.Figure()
            for col, name, colour in (("our_mae", "Our forecast", ORANGE),
                                      ("neso_mae", "NESO's forecast", AQUA)):  # fmt: skip
                fig.add_bar(x=bands.label, y=bands[col], name=name, marker_color=colour,
                            text=bands[col].round(0), textposition="outside",
                            hovertemplate="%{x}: off by %{y:.0f} gCO₂/kWh")  # fmt: skip
            fig.update_layout(
                title="Average miss by how far ahead (lower is better)",
                yaxis_title="gCO₂/kWh",
                barmode="group",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig, width="stretch")

            example = q(EXAMPLE_SQL)
            if not example.empty:  # none yet in a forecast's first 48 h
                fig = go.Figure()
                for col, name, colour, dash in (
                    ("actual_gco2_kwh", "What actually happened", BLUE, "solid"),
                    ("model_gco2_kwh", "Our forecast", ORANGE, "solid"),
                    ("neso_gco2_kwh", "NESO's forecast", AQUA, "dot"),
                ):
                    fig.add_scatter(x=uk(example.period_from), y=example[col], name=name,
                                    line=dict(color=colour, width=2, dash=dash))  # fmt: skip
                fig.update_layout(
                    title="Example: the forecast made "
                    f"{uk(example.issued_at.iloc[0]):%a %d %b, %H:%M} vs what happened (UK time)",
                    yaxis_title="gCO₂/kWh",
                    hovermode="x unified",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                st.plotly_chart(fig, width="stretch")
                st.caption("The most recent forecast whose whole 48 hours have now happened.")

            with st.expander("Detailed numbers and what they mean"):
                st.markdown(
                    "- **Typically off by** (MAE, mean absolute error): the average gap between "
                    "forecast and actual, whether too high or too low.\n"
                    "- **Bias**: whether a forecast runs high (+) or low (−) on average.\n"
                    "- **We were closer**: the share of half-hours where our forecast was nearer "
                    "the actual than NESO's.\n"
                    "- **gCO₂/kWh**: grams of CO₂ emitted per kilowatt-hour of electricity."
                )
                table = scored.assign(band=scored.band.map(PLAIN_BANDS))
                first_day = uk(table.first_target).dt.strftime("%d %b")
                table["targets"] = first_day + " – " + uk(table.last_target).dt.strftime("%d %b")
                g = st.column_config.NumberColumn
                st.dataframe(
                    table,
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
                    ],  # fmt: skip
                    column_config={
                        "band": "How far ahead",
                        "issues": g("Forecasts", help="Forecasts scored in this band"),
                        "n": g("Half-hours", help="Half-hours with an actual and both forecasts"),
                        "targets": "Dates covered",
                        "our_mae": g("Ours: typically off by", format="%.1f"),
                        "neso_mae": g("NESO: typically off by", format="%.1f"),
                        "gap": g("Ours − NESO", format="%+.1f", help="Negative = ours was better"),
                        "we_closer": g("We were closer", format="%.0f%%"),
                        "our_bias": g("Our bias", format="%+.1f", help="+ = forecast ran high"),
                        "neso_bias": g("NESO bias", format="%+.1f", help="+ = forecast ran high"),
                    },
                )
                first, last = uk(scored.first_target.min()), uk(scored.last_target.max())
                days = (last.date() - first.date()).days + 1  # calendar dates spanned, UK time
                if days < 28:
                    st.caption(f"Only {days} days of live data so far, and each row covers "
                               "different dates, so compare rows with care.")  # fmt: skip
        st.caption(backtest_caption())

    with about_tab:
        st.markdown(f"""
**How it works.** A Python pipeline pulls the NESO Carbon Intensity API and Open-Meteo weather into
MotherDuck every few hours (GitHub Actions); dbt models and tests the data; a LightGBM model
forecasts the next 48 hours; this app reads the result.
[Code]({REPO}) · [dbt docs](https://p3rpetualpanda.github.io/gb-grid-carbon/)

**Data:** Carbon Intensity API, National Energy System Operator (CC BY 4.0); wind and solar
forecasts from the NESO Data Portal (NESO Open Data Licence); weather by Open-Meteo.com
(CC BY 4.0). Regional figures are forecasts (NESO publishes no regional actuals).
""")


if __name__ == "__main__":  # Streamlit runs the script as __main__; tests import the helpers only
    main()
