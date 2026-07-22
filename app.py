"""
app.py — ResultsEdge: NSE Earnings Intelligence Platform
Tabs: Leaderboard | Company Deep-dive | Sector View | Pipeline
"""
import json
import logging
import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from modules import db
from modules import pipeline as pl
from modules import equisense as eq

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ResultsEdge",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

logging.basicConfig(level=logging.INFO)

# ── constants ─────────────────────────────────────────────────────────────────
SCORE_COLS = [
    ("s1_revenue_growth",     "S1 Rev"),
    ("s2_profitability",      "S2 Profit"),
    ("s3_cashflow",           "S3 CFO"),
    ("s4_guidance_quality",   "S4 Guidance"),
    ("s5_walk_the_talk",      "S5 WTT"),
    ("s6_current_delivery",   "S6 Delivery"),
    ("s7_sector_position",    "S7 Sector"),
    ("s8_sector_opportunity", "S8 Opp"),
    ("s9_momentum",           "S9 Momentum"),
    ("s10_valuation",         "S10 Value"),
]

SCORE_DESCRIPTIONS = {
    "s1_revenue_growth":     "Revenue growth quality — YoY %, trend direction over 4 quarters",
    "s2_profitability":      "Profitability expansion — OPM change vs 4Q avg, PAT growth",
    "s3_cashflow":           "Cash flow quality — CFO/PAT ratio",
    "s4_guidance_quality":   "Guidance specificity — quantified vs vague, metrics covered",
    "s5_walk_the_talk":      "Historical delivery vs guidance — last 4 quarters",
    "s6_current_delivery":   "Current quarter actual vs prior concall guidance",
    "s7_sector_position":    "Revenue growth & margin rank vs all sector peers",
    "s8_sector_opportunity": "Sector tailwinds — management tone, positives vs risks",
    "s9_momentum":           "RS-63d vs Nifty, price vs 200DMA, 52-week percentile rank",
    "s10_valuation":         "EPS growth trajectory, P/E vs sector context",
}

RATING_COLORS = {
    "Excellent": "#1B8A4B",
    "Strong":    "#2E86DE",
    "Average":   "#F5A623",
    "Weak":      "#E8520A",
    "Poor":      "#C0392B",
}

WEIGHTS = {
    "s1": 10, "s2": 10, "s3": 5,
    "s4": 10, "s5": 20, "s6": 10,
    "s7": 10, "s8": 5,  "s9": 10, "s10": 10,
}

RAT_MAP = {
    "s1_revenue_growth":     "s1_rationale",
    "s2_profitability":      "s2_rationale",
    "s3_cashflow":           "s3_rationale",
    "s4_guidance_quality":   "s4_rationale",
    "s5_walk_the_talk":      "s5_rationale",
    "s6_current_delivery":   "s6_rationale",
    "s7_sector_position":    "s7_rationale",
    "s8_sector_opportunity": "s8_rationale",
    "s9_momentum":           "s9_rationale",
    "s10_valuation":         "s10_rationale",
}

QUARTERS_LIST = ["Q1FY27","Q4FY26","Q3FY26","Q2FY26","Q1FY26",
                  "Q4FY25","Q3FY25","Q2FY25","Q1FY25"]


# ── helpers ───────────────────────────────────────────────────────────────────

def _rating_badge(label: str) -> str:
    color = RATING_COLORS.get(label, "#888")
    return (f'<span style="background:{color};color:#fff;padding:3px 12px;'
            f'border-radius:12px;font-size:0.8rem;font-weight:600">{label}</span>')


def _score_bar_html(val: float) -> str:
    pct = min(float(val) / 10 * 100, 100)
    if pct >= 80:   c = "#1B8A4B"
    elif pct >= 60: c = "#2E86DE"
    elif pct >= 40: c = "#F5A623"
    else:           c = "#C0392B"
    return (f'<div style="width:100%;background:#E9EEF4;border-radius:4px;height:7px;margin:4px 0">'
            f'<div style="width:{pct:.0f}%;background:{c};height:7px;border-radius:4px"></div>'
            f'</div><small style="color:#666">{val:.1f}/10</small>')


def _parse_membership(val) -> list:
    """Safely parse index_membership whether it's a list or JSON string."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _active_quarter() -> str:
    return db.get_config("active_quarter", "Q4FY26")


def _render_equisense_panel(ticker: str, quarter: str, company_name: str) -> None:
    """
    Render the EquiSense AI narrative panel — a qualitative overlay
    sourced independently of Screener/BSE-concall data.

    IMPORTANT: This is display-only. Nothing here feeds re_scores or
    any S1–S10 parameter. Kept as a separate, clearly-labeled source
    so provenance is unambiguous and quarter-over-quarter scores stay
    comparable (Screener/concall-only).

    Data is pre-cached in re_equisense_cache (populated out-of-band —
    see modules/equisense.py docstring). This function only reads the
    cache; it never calls the EquiSense API directly, since that call
    is only available via the chat-side MCP connector, not from a
    deployed Streamlit backend.
    """
    st.markdown("#### 🧭 AI Research Narrative")
    st.caption(
        "Sourced from EquiSense — an independent AI research layer. "
        "Qualitative context only; does not factor into the parameter scores above."
    )

    cached = eq.get_cached(ticker, quarter)
    if not cached:
        st.info(
            f"No cached EquiSense narrative for {company_name} ({quarter}) yet. "
            f"Ask Claude to refresh EquiSense coverage for this ticker."
        )
        return

    fetched_at = cached.get("fetched_at", "")
    st.caption(f"Last refreshed: {fetched_at[:16].replace('T', ' ')} UTC")
    st.markdown(cached.get("answer_text", ""))

    follow_ups = cached.get("follow_ups") or []
    if isinstance(follow_ups, str):
        try:
            follow_ups = json.loads(follow_ups)
        except Exception:
            follow_ups = []
    if follow_ups:
        st.markdown("**Suggested follow-ups:**")
        for fu in follow_ups:
            st.markdown(f"- {fu}")

    st.caption("🚀 Powered by equisense.ai")


# ── leaderboard tab ───────────────────────────────────────────────────────────

def tab_leaderboard():
    st.subheader("🏆 Leaderboard")

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        aq = _active_quarter()
        quarter = st.selectbox("Quarter", QUARTERS_LIST,
                               index=QUARTERS_LIST.index(aq) if aq in QUARTERS_LIST else 0,
                               key="lb_quarter")
    with col2:
        all_cos = db.get_all_companies()
        sectors = ["All"] + sorted({c.get("sector", "—") for c in all_cos})
        sector_filter = st.selectbox("Sector", sectors, key="lb_sector")
    with col3:
        index_filter = st.selectbox("Index", ["nifty50", "nifty200", "nifty500"],
                                    key="lb_index")
    with col4:
        min_score = st.number_input("Min score", 0, 100, 0, step=5, key="lb_minscore")

    rows = db.get_scores_leaderboard(quarter)
    if not rows:
        st.info(f"No scores for {quarter}. Go to ⚙️ Pipeline → Run scoring.")
        return

    # apply filters
    if sector_filter != "All":
        rows = [r for r in rows if r.get("sector") == sector_filter]
    rows = [r for r in rows
            if index_filter in _parse_membership(r.get("index_membership"))]
    rows = [r for r in rows if (r.get("composite_score") or 0) >= min_score]

    if not rows:
        st.warning("No companies match the current filters.")
        return

    sort_by = st.selectbox("Sort by",
                            ["Score"] + [lbl for _, lbl in SCORE_COLS],
                            key="lb_sortby")

    records = []
    for i, r in enumerate(rows, 1):
        rec = {
            "Rank":    i,
            "Ticker":  r["ticker"],
            "Company": (r.get("name") or "—")[:28],
            "Sector":  r.get("sector") or "—",
            "Score":   r.get("composite_score") or 0.0,
            "Rating":  r.get("rating_label") or "—",
        }
        for col_key, col_label in SCORE_COLS:
            rec[col_label] = float(r.get(col_key) or 0)
        records.append(rec)

    df = pd.DataFrame(records)
    df = df.sort_values(sort_by, ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)

    # Use ProgressColumn for score visualisation — no Styler needed
    col_cfg = {
        "Score": st.column_config.ProgressColumn(
            "Score /100", min_value=0, max_value=100, format="%.1f"),
    }
    for _, lbl in SCORE_COLS:
        col_cfg[lbl] = st.column_config.ProgressColumn(
            lbl, min_value=0, max_value=10, format="%.1f")

    st.dataframe(df, use_container_width=True, height=600,
                 column_config=col_cfg, hide_index=True)
    st.caption(
        f"Showing {len(df)} companies · "
        f"Last refresh: {db.get_config('last_full_run', 'never')}"
    )


# ── company deep-dive tab ─────────────────────────────────────────────────────

def tab_company():
    st.subheader("🔍 Company Deep-Dive")

    all_cos = db.get_all_companies()
    if not all_cos:
        st.info("No companies loaded. Go to ⚙️ Pipeline → Seed companies.")
        return

    tickers = sorted([c["ticker"] for c in all_cos])
    c1, c2  = st.columns([2, 2])
    with c1:
        ticker = st.selectbox("Select company", tickers, key="co_ticker")
    with c2:
        aq      = _active_quarter()
        quarter = st.selectbox("Quarter", QUARTERS_LIST,
                               index=QUARTERS_LIST.index(aq) if aq in QUARTERS_LIST else 0,
                               key="co_quarter")

    co           = next((c for c in all_cos if c["ticker"] == ticker), {})
    score        = db.get_score(ticker, quarter)
    fins         = db.get_financials(ticker, limit=8)
    concall      = db.get_concall(ticker, quarter)
    guidance     = db.get_guidance_history(ticker, limit=20)
    company_name = co.get("name", ticker)
    sector       = co.get("sector", "—")

    # header row
    h1, h2, h3 = st.columns([3, 1, 1])
    with h1:
        st.markdown(f"### {company_name} ({ticker})")
        st.caption(f"{sector} · BSE {co.get('bse_code','—')}")
    with h2:
        if score:
            st.metric("Composite Score",
                      f"{score.get('composite_score', '—')}/100")
            st.markdown(_rating_badge(score.get("rating_label","—")),
                        unsafe_allow_html=True)
        else:
            st.metric("Composite Score", "—")
    with h3:
        completeness = float(score.get("data_completeness", 0)) if score else 0.0
        st.metric("Data completeness", f"{completeness:.0f}%")

    st.divider()
    left, right = st.columns(2)

    # ── scorecard ─────────────────────────────────────────────────────────
    with left:
        st.markdown("#### Parameter Scores")
        if score:
            for col_key, col_label in SCORE_COLS:
                v      = float(score.get(col_key) or 0)
                rat    = score.get(RAT_MAP.get(col_key, "")) or "—"
                weight = WEIGHTS.get(col_key.split("_")[0], 0)
                with st.expander(f"{col_label}  ·  {v:.1f}/10  (weight {weight}%)",
                                  expanded=False):
                    st.markdown(_score_bar_html(v), unsafe_allow_html=True)
                    st.caption(SCORE_DESCRIPTIONS.get(col_key, ""))
                    st.write(rat)

            # radar chart
            cats   = [lbl for _, lbl in SCORE_COLS]
            values = [float(score.get(k) or 0) for k, _ in SCORE_COLS]
            fig_r  = go.Figure(go.Scatterpolar(
                r=values + [values[0]],
                theta=cats + [cats[0]],
                fill="toself",
                line_color="#378ADD",
                fillcolor="rgba(55,138,221,0.18)",
            ))
            fig_r.update_layout(
                polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
                showlegend=False,
                margin=dict(l=20, r=20, t=30, b=20),
                height=300,
            )
            st.plotly_chart(fig_r, use_container_width=True)
        else:
            st.info(f"No score for {ticker} {quarter}. Run scoring from ⚙️ Pipeline.")

    # ── financial charts ───────────────────────────────────────────────────
    with right:
        if fins:
            qs   = [f["quarter"]     for f in fins][::-1]
            revs = [float(f.get("revenue_cr") or 0) for f in fins][::-1]
            opms = [float(f.get("opm_pct")    or 0) for f in fins][::-1]
            pats = [float(f.get("pat_cr")     or 0) for f in fins][::-1]

            for title, ydata, color in [
                ("Revenue (₹ Cr)",    revs, "#378ADD"),
                ("EBITDA Margin %",   opms, "#1B8A4B"),
                ("PAT (₹ Cr)",        pats, "#F5A623"),
            ]:
                is_line = "Margin" in title
                if is_line:
                    fig = px.line(x=qs, y=ydata, title=title, markers=True,
                                  color_discrete_sequence=[color])
                else:
                    fig = px.bar(x=qs, y=ydata, title=title,
                                 color_discrete_sequence=[color])
                fig.update_layout(height=210,
                                   margin=dict(l=10, r=10, t=30, b=10),
                                   xaxis_title="", yaxis_title="")
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Financial data not yet loaded. Run Financials pipeline.")

    st.divider()

    # ── concall summary ────────────────────────────────────────────────────
    if concall:
        st.markdown(f"#### 📋 Concall Summary — {quarter}")
        c1, c2 = st.columns([3, 1])
        with c1:
            st.write(concall.get("ai_summary") or "Summary not available.")
            if concall.get("sector_outlook"):
                st.markdown("**Sector outlook:**")
                st.write(concall["sector_outlook"])
        with c2:
            pos   = concall.get("key_positives") or []
            risks = concall.get("key_risks")     or []
            if isinstance(pos,  str):
                try: pos   = json.loads(pos)
                except: pos = []
            if isinstance(risks, str):
                try: risks = json.loads(risks)
                except: risks = []
            if pos:
                st.markdown("**✅ Key positives**")
                for p in pos:
                    st.markdown(f"- {p}")
            if risks:
                st.markdown("**⚠️ Key risks**")
                for rk in risks:
                    st.markdown(f"- {rk}")
    else:
        st.info(f"No concall data for {ticker} {quarter}. Run Concall pipeline.")

    st.divider()

    # ── EquiSense AI narrative (qualitative overlay — separate source) ─────
    _render_equisense_panel(ticker, quarter, company_name)

    st.divider()

    # ── guidance history table ─────────────────────────────────────────────
    if guidance:
        st.markdown("#### 🎯 Walk-the-Talk — Guidance vs Actuals")
        OUTCOME = {
            2: "🟢 Major beat", 1: "✅ Met / minor beat",
            0: "🟡 Met",        -1: "🔴 Minor miss", -2: "❌ Major miss",
        }
        g_rows = [{
            "Guided in":        g.get("guidance_given_quarter"),
            "For period":       g.get("for_period"),
            "Metric":           g.get("metric"),
            "Type":             g.get("guidance_type"),
            "Direction guided": g.get("guided_direction"),
            "Magnitude":        g.get("guided_magnitude"),
            "Guided %":         g.get("guided_value_pct"),
            "Actual %":         g.get("actual_pct"),
            "Outcome":          OUTCOME.get(g.get("hit_miss_score"),
                                             "⏳ Pending" if g.get("hit_miss_score") is None
                                             else "—"),
        } for g in guidance]
        st.dataframe(pd.DataFrame(g_rows), use_container_width=True)
    else:
        st.info("Guidance history empty. Run Concall pipeline then Scoring.")


# ── sector view tab ───────────────────────────────────────────────────────────

def tab_sector():
    st.subheader("📊 Sector View")

    all_cos = db.get_all_companies()
    sectors = sorted({c.get("sector", "—") for c in all_cos})
    if not sectors:
        st.info("No companies loaded.")
        return

    c1, c2 = st.columns(2)
    with c1:
        sector = st.selectbox("Select sector", sectors, key="sv_sector")
    with c2:
        aq      = _active_quarter()
        quarter = st.selectbox("Quarter", QUARTERS_LIST,
                               index=QUARTERS_LIST.index(aq) if aq in QUARTERS_LIST else 0,
                               key="sv_quarter")

    ticker_list  = [c["ticker"] for c in all_cos if c.get("sector") == sector]
    if not ticker_list:
        st.warning("No companies in this sector.")
        return

    all_scores    = db.get_scores_leaderboard(quarter)
    sector_scores = [s for s in all_scores if s["ticker"] in ticker_list]
    if not sector_scores:
        st.info(f"No scores for {sector} in {quarter}.")
        return

    composites = [float(s.get("composite_score") or 0) for s in sector_scores]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Companies scored", len(sector_scores))
    m2.metric("Sector avg",  f"{sum(composites)/len(composites):.1f}")
    m3.metric("Highest",     f"{max(composites):.1f}")
    m4.metric("Lowest",      f"{min(composites):.1f}")

    st.divider()

    records = []
    for i, s in enumerate(sorted(sector_scores,
                                   key=lambda x: x.get("composite_score") or 0,
                                   reverse=True), 1):
        rec = {
            "Rank":    i,
            "Ticker":  s["ticker"],
            "Company": (s.get("name") or "—")[:28],
            "Score":   float(s.get("composite_score") or 0),
            "Rating":  s.get("rating_label") or "—",
        }
        for col_key, col_label in SCORE_COLS:
            rec[col_label] = float(s.get(col_key) or 0)
        records.append(rec)

    df = pd.DataFrame(records)
    col_cfg = {
        "Score": st.column_config.ProgressColumn(
            "Score /100", min_value=0, max_value=100, format="%.1f"),
    }
    for _, lbl in SCORE_COLS:
        col_cfg[lbl] = st.column_config.ProgressColumn(
            lbl, min_value=0, max_value=10, format="%.1f")

    st.dataframe(df, use_container_width=True, column_config=col_cfg,
                 hide_index=True)

    st.divider()
    st.markdown("#### Score comparison")
    fig = px.bar(
        df.sort_values("Score"),
        x="Score", y="Ticker", orientation="h",
        color="Score",
        color_continuous_scale=[[0,"#C0392B"],[0.4,"#F5A623"],
                                  [0.7,"#2E86DE"],[1.0,"#1B8A4B"]],
        title=f"{sector} — Composite scores ({quarter})",
        text="Score",
    )
    fig.update_layout(height=max(300, 40 * len(df)),
                       coloraxis_showscale=False,
                       yaxis_title="", xaxis_range=[0, 100])
    fig.update_traces(texttemplate="%{text:.1f}", textposition="outside")
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### Walk-the-Talk vs Revenue Growth")
    sdf = pd.DataFrame([{
        "Ticker":               s["ticker"],
        "Walk the Talk (S5)":   float(s.get("s5_walk_the_talk")   or 0),
        "Revenue Growth (S1)":  float(s.get("s1_revenue_growth")  or 0),
        "Score":                float(s.get("composite_score")     or 0),
    } for s in sector_scores])
    fig2 = px.scatter(
        sdf, x="Revenue Growth (S1)", y="Walk the Talk (S5)",
        size="Score", text="Ticker",
        title=f"{sector} — Delivery vs Growth ({quarter})",
        size_max=30, color="Score",
        color_continuous_scale=[[0,"#C0392B"],[0.5,"#F5A623"],[1.0,"#1B8A4B"]],
    )
    fig2.update_traces(textposition="top center")
    fig2.update_layout(height=400, xaxis_range=[0, 10], yaxis_range=[0, 10])
    st.plotly_chart(fig2, use_container_width=True)


# ── pipeline tab ──────────────────────────────────────────────────────────────

def tab_pipeline():
    st.subheader("⚙️ Data Pipeline")

    with st.expander("🔧 Config", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            aq    = _active_quarter()
            new_q = st.text_input("Active quarter", value=aq)
            if st.button("Update quarter"):
                db.set_config("active_quarter", new_q)
                st.success(f"Active quarter → {new_q}")
        with c2:
            st.write("**Supabase:** aiebaqvclyzxajigvkfd (ap-south-1)")
            st.write("**Tables:** re_companies · re_financials · re_concall")
            st.write("           re_guidance · re_scores · re_technical")

    st.divider()

    # ── concall cache status ───────────────────────────────────────────────
    st.markdown("#### 📊 Concall Cache Status")
    aq = _active_quarter()
    try:
        all_parsed = db.get_client().table("re_concall")\
                       .select("ticker,quarter,parsed_at")\
                       .execute().data or []
        parsed_this_q = {r["ticker"] for r in all_parsed
                         if r.get("quarter") == aq and r.get("parsed_at")}
        all_cos = db.get_all_companies()
        total_cos = len(all_cos)
        st.info(
            f"**{aq}:** {len(parsed_this_q)}/{total_cos} companies already parsed "
            f"and cached in Supabase. "
            f"Running Concalls will **skip** all {len(parsed_this_q)} cached — "
            f"only {total_cos - len(parsed_this_q)} new API calls needed."
        )
        if parsed_this_q:
            with st.expander(f"✅ Already cached for {aq} ({len(parsed_this_q)} companies)", expanded=False):
                st.write(", ".join(sorted(parsed_this_q)))
    except Exception:
        all_cos = db.get_all_companies()
        parsed_this_q = set()

    tickers = [c["ticker"] for c in all_cos] if all_cos else []

    st.divider()
    st.markdown("#### Run Pipeline Steps")

    # ── batch and index controls ───────────────────────────────────────────
    with st.expander("⚙️ Batch & Index settings", expanded=True):
        bs1, bs2, bs3, bs4 = st.columns(4)
        with bs1:
            index_choice = st.selectbox(
                "Index universe",
                ["nifty50", "nifty200"],
                key="pipe_index",
                help="nifty50 = 50 companies, nifty200 = all 200"
            )
        with bs2:
            batch_size = st.number_input(
                "Batch size", min_value=1, max_value=200, value=10, step=5,
                key="pipe_batch",
                help="Companies per run. Use 10 for concalls, 50 for financials"
            )
        with bs3:
            batch_offset = st.number_input(
                "Start from #", min_value=1, max_value=200, value=1, step=1,
                key="pipe_offset",
                help="Start from company #N. Use to resume mid-way"
            )
        with bs4:
            force_refresh = st.toggle(
                "Force re-parse",
                value=False,
                key="pipe_force",
                help="OFF = skip companies already in DB (saves API tokens). ON = re-parse everything"
            )

        # Filter by selected index
        index_tickers = [
            c["ticker"] for c in all_cos
            if index_choice in _parse_membership(c.get("index_membership"))
        ]
        batch_tickers = index_tickers[int(batch_offset)-1 : int(batch_offset)-1+int(batch_size)]

        not_cached = [t for t in batch_tickers if t not in parsed_this_q]
        st.caption(
            f"📋 Universe: **{len(index_tickers)}** companies ({index_choice}) · "
            f"Batch: **{len(batch_tickers)}** companies · "
            f"Will call Claude API for: **{len(not_cached)}** "
            f"(skipping {len(batch_tickers)-len(not_cached)} already cached)"
        )
        if batch_tickers:
            st.caption(f"Batch: {', '.join(batch_tickers)}")

    st.divider()

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        seed_idx = st.selectbox("Seed index", ["all","nifty50","nifty200"],
                                key="pipe_seed_idx", label_visibility="collapsed")
        if st.button("🌱 Seed", use_container_width=True,
                     help="Seed nifty50, nifty200, or all companies into DB"):
            with st.spinner(f"Seeding {seed_idx}..."):
                n = pl.seed_companies(index=seed_idx)
            st.success(f"Seeded {n}")

    with col2:
        if st.button("📑 Financials", use_container_width=True,
                     help="Fetch quarterly P&L from Screener.in"):
            prog = st.progress(0)
            def _cb2(i, total, t):
                prog.progress((i+1)/total, text=f"{t} ({i+1}/{total})")
            with st.spinner(f"Fetching {len(batch_tickers)} companies..."):
                r = pl.run_financials(batch_tickers, progress_cb=_cb2)
            prog.empty()
            st.success(f"✅ {len(r['processed'])} ok, {len(r['failed'])} failed")
            if r["failed"]:
                st.warning(f"Failed: {', '.join(str(x) for x in r['failed'])}")

    with col3:
        if st.button("🎙️ Concalls", use_container_width=True,
                     help="Parse concall PDFs via Claude AI. Skips already-cached companies unless Force re-parse is ON"):
            prog = st.progress(0)
            def _cb3(i, total, t):
                prog.progress((i+1)/total, text=f"{t} ({i+1}/{total})")
            with st.spinner(f"Parsing {len(batch_tickers)} companies "
                            f"({'force refresh' if force_refresh else 'smart cache'})..."):
                r = pl.run_concalls(batch_tickers,
                                    force_refresh=force_refresh,
                                    progress_cb=_cb3)
            prog.empty()
            st.success(f"✅ {len(r['processed'])} companies, "
                       f"{r.get('quarters_parsed',0)} quarters")
            if r["failed"]:
                st.warning(f"Failed: {', '.join(str(x) for x in r['failed'])}")

    with col4:
        if st.button("📈 Technicals", use_container_width=True,
                     help="Fetch Breeze API price/momentum data"):
            prog = st.progress(0)
            def _cb4(i, total, t):
                prog.progress((i+1)/total, text=f"{t} ({i+1}/{total})")
            with st.spinner(f"Fetching {len(batch_tickers)} companies..."):
                r = pl.run_technicals(batch_tickers, progress_cb=_cb4)
            prog.empty()
            st.success(f"✅ {len(r['processed'])} processed")
            if r["failed"]:
                st.warning(f"Failed: {', '.join(str(x) for x in r['failed'])}")

    with col5:
        if st.button("🧮 Score", use_container_width=True, type="primary",
                     help="Compute all 10 parameters + composite score"):
            quarter = _active_quarter()
            prog    = st.progress(0)
            def _cb5(i, total, t):
                prog.progress((i+1)/total, text=f"{t} ({i+1}/{total})")
            with st.spinner(f"Scoring {quarter}..."):
                r = pl.run_scoring(index_tickers, quarter, progress_cb=_cb5)
            prog.empty()
            db.set_config("last_full_run",
                          datetime.datetime.now().strftime("%d %b %Y %H:%M"))
            st.success(f"✅ {len(r['processed'])} scored")

    st.divider()
    st.markdown("#### 🚀 Full Pipeline (all steps, uses batch settings above)")
    if st.button("▶ Run full pipeline", type="primary"):
        with st.status("Running...", expanded=True) as status:
            def _cbf(i, total, t):
                st.write(f"→ {t} ({i+1}/{total})")
            result = pl.run_full(tickers=batch_tickers, progress_cb=_cbf)
            db.set_config("last_full_run",
                          datetime.datetime.now().strftime("%d %b %Y %H:%M"))
            status.update(label="✅ Complete!", state="complete")
        for step, rv in result.items():
            if isinstance(rv, dict) and "error" not in rv:
                st.success(f"{step}: {rv}")
            elif isinstance(rv, dict) and "error" in rv:
                st.error(f"{step}: {rv['error']}")

    st.divider()
    st.markdown("#### 📋 Recent Pipeline Logs")
    logs = db.get_recent_logs(20)
    if logs:
        log_rows = [{
            "Run type":  lg.get("run_type"),
            "Status":    lg.get("status"),
            "Processed": len(lg.get("tickers_processed") or []),
            "Failed":    len(lg.get("tickers_failed")    or []),
            "Records":   lg.get("records_inserted"),
            "Duration":  (f"{lg.get('duration_seconds')}s"
                          if lg.get('duration_seconds') is not None else "—"),
            "Started":   (lg.get("started_at","")[:16].replace("T"," ")
                          if lg.get("started_at") else "—"),
        } for lg in logs]
        st.dataframe(pd.DataFrame(log_rows), use_container_width=True,
                     height=300, hide_index=True)
    else:
        st.info("No pipeline runs yet.")

    with st.expander("📋 Company master list", expanded=False):
        if all_cos:
            def _idx_str(c):
                m = _parse_membership(c.get("index_membership"))
                return ", ".join(m) if m else "—"
            cos_df = pd.DataFrame([{
                "Ticker": c["ticker"],
                "Name":   c.get("name","—"),
                "Sector": c.get("sector","—"),
                "BSE":    c.get("bse_code","—"),
                "Index":  _idx_str(c),
            } for c in sorted(all_cos, key=lambda x: x["ticker"])])
            st.dataframe(cos_df, use_container_width=True, hide_index=True)
        else:
            st.info("Run 🌱 Seed companies first.")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    st.markdown(
        "<h1 style='color:#042C53;margin-bottom:0'>📊 ResultsEdge</h1>"
        "<p style='color:#378ADD;margin-top:0;font-size:1.05rem'>"
        "NSE Earnings Intelligence Platform · Nifty 50 → 500</p>",
        unsafe_allow_html=True,
    )

    tab1, tab2, tab3, tab4 = st.tabs([
        "🏆 Leaderboard",
        "🔍 Company Deep-Dive",
        "📊 Sector View",
        "⚙️ Pipeline",
    ])
    with tab1: tab_leaderboard()
    with tab2: tab_company()
    with tab3: tab_sector()
    with tab4: tab_pipeline()


if __name__ == "__main__":
    main()
