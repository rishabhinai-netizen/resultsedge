"""
app.py — ResultsEdge: NSE Earnings Intelligence Platform
Tabs: Leaderboard | Company Deep-dive | Sector View | Pipeline
"""
import json
import logging

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from modules import db
from modules import pipeline as pl

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
    ("s1_revenue_growth",    "S1 Rev"),
    ("s2_profitability",     "S2 Profit"),
    ("s3_cashflow",          "S3 CFO"),
    ("s4_guidance_quality",  "S4 Guidance"),
    ("s5_walk_the_talk",     "S5 WTT ⭐"),
    ("s6_current_delivery",  "S6 Delivery"),
    ("s7_sector_position",   "S7 Sector"),
    ("s8_sector_opportunity","S8 Opp"),
    ("s9_momentum",          "S9 Momentum"),
    ("s10_valuation",        "S10 Value"),
]

SCORE_DESCRIPTIONS = {
    "s1_revenue_growth":     "Revenue growth quality — YoY %, trend direction over 4 quarters",
    "s2_profitability":      "Profitability expansion — OPM change vs 4Q avg, PAT growth",
    "s3_cashflow":           "Cash flow quality — CFO/PAT ratio, FCF health",
    "s4_guidance_quality":   "Guidance specificity — quantified vs vague, metrics covered",
    "s5_walk_the_talk":      "Historical delivery vs guidance — last 4 quarters, direction + magnitude",
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


# ── styling helpers ───────────────────────────────────────────────────────────

def _color_score(val):
    try:
        v = float(val)
    except (TypeError, ValueError):
        return ""
    if v >= 8:   return "background-color:#E6F4EA; color:#1B8A4B"
    if v >= 6:   return "background-color:#E8F3FF; color:#1355A0"
    if v >= 4:   return "background-color:#FFF8E6; color:#8A5A00"
    return "background-color:#FFF0EE; color:#C0392B"


def _rating_badge(label: str) -> str:
    color = RATING_COLORS.get(label, "#888")
    return (f'<span style="background:{color};color:#fff;padding:2px 10px;'
            f'border-radius:12px;font-size:0.78rem;font-weight:600">{label}</span>')


def _score_bar(val: float, max_val: float = 10) -> str:
    pct = min(val / max_val * 100, 100)
    if pct >= 80:   c = "#1B8A4B"
    elif pct >= 60: c = "#2E86DE"
    elif pct >= 40: c = "#F5A623"
    else:           c = "#C0392B"
    return (f'<div style="width:100%;background:#E9EEF4;border-radius:4px;height:8px">'
            f'<div style="width:{pct:.0f}%;background:{c};height:8px;border-radius:4px"></div>'
            f'</div><small style="color:#555">{val:.1f}/10</small>')


# ── tab: leaderboard ──────────────────────────────────────────────────────────

def tab_leaderboard():
    st.subheader("🏆 Leaderboard")

    # Controls
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        quarters_opt = ["Q4FY26", "Q3FY26", "Q2FY26", "Q1FY26",
                         "Q4FY25", "Q3FY25", "Q2FY25", "Q1FY25"]
        active_q = db.get_config("active_quarter", "Q4FY26")
        quarter  = st.selectbox("Quarter", quarters_opt,
                                index=quarters_opt.index(active_q) if active_q in quarters_opt else 0)
    with col2:
        sectors = ["All"] + sorted({
            c.get("sector","—") for c in db.get_all_companies()
        })
        sector_filter = st.selectbox("Sector", sectors)
    with col3:
        index_filter = st.selectbox("Index", ["nifty50", "nifty200", "nifty500"])
    with col4:
        min_score = st.number_input("Min score", 0, 100, 0, step=5)

    rows = db.get_scores_leaderboard(quarter)
    if not rows:
        st.info(f"No scores found for {quarter}. Run the pipeline from the ⚙️ Pipeline tab.")
        return

    # Filter
    if sector_filter != "All":
        rows = [r for r in rows if r.get("sector") == sector_filter]

    def _has_index(r, idx):
        membership = r.get("index_membership") or []
        if isinstance(membership, str):
            try:
                import json as _j
                membership = _j.loads(membership)
            except Exception:
                membership = []
        return idx in membership

    rows = [r for r in rows if _has_index(r, index_filter)]
    rows = [r for r in rows if (r.get("composite_score") or 0) >= min_score]

    if not rows:
        st.warning("No companies match the current filters.")
        return

    # Build display dataframe
    records = []
    for i, r in enumerate(rows, 1):
        rec = {
            "Rank":    i,
            "Ticker":  r["ticker"],
            "Company": r.get("name", "—")[:28],
            "Sector":  r.get("sector", "—"),
            "Score":   r.get("composite_score"),
            "Rating":  r.get("rating_label", "—"),
        }
        for col_key, col_label in SCORE_COLS:
            rec[col_label] = r.get(col_key)
        records.append(rec)

    df = pd.DataFrame(records)

    # Sort control
    sort_by = st.selectbox(
        "Sort by",
        ["Score"] + [lbl for _, lbl in SCORE_COLS],
    )
    df = df.sort_values(sort_by, ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)

    score_display_cols = [lbl for _, lbl in SCORE_COLS] + ["Score"]
    styled = df.style.applymap(_color_score, subset=score_display_cols)
    st.dataframe(styled, use_container_width=True, height=600,
                 column_config={
                     "Score": st.column_config.NumberColumn("Score (/100)", format="%.1f"),
                     **{lbl: st.column_config.NumberColumn(lbl, format="%.1f")
                        for _, lbl in SCORE_COLS}
                 })
    st.caption(f"Showing {len(df)} companies. Click column headers to sort. "
               f"Last refresh: {db.get_config('last_full_run', 'never')}.")


# ── tab: company deep-dive ────────────────────────────────────────────────────

def tab_company():
    st.subheader("🔍 Company Deep-Dive")

    companies = db.get_all_companies()
    if not companies:
        st.info("No companies loaded. Run pipeline seed first.")
        return

    tickers     = sorted([c["ticker"] for c in companies])
    col1, col2  = st.columns([2, 2])
    with col1:
        ticker = st.selectbox("Select company", tickers)
    with col2:
        quarters_opt = ["Q4FY26", "Q3FY26", "Q2FY26", "Q1FY26",
                         "Q4FY25", "Q3FY25", "Q2FY25", "Q1FY25"]
        active_q = db.get_config("active_quarter", "Q4FY26")
        quarter  = st.selectbox("Quarter", quarters_opt,
                                index=quarters_opt.index(active_q) if active_q in quarters_opt else 0)

    co      = next((c for c in companies if c["ticker"] == ticker), {})
    score   = db.get_score(ticker, quarter)
    fins    = db.get_financials(ticker, limit=8)
    concall = db.get_concall(ticker, quarter)
    guidance= db.get_guidance_history(ticker, limit=20)

    company_name = co.get("name", ticker)
    sector       = co.get("sector", "—")

    # ── header ─────────────────────────────────────────────────────────────
    h1, h2, h3 = st.columns([3, 1, 1])
    with h1:
        st.markdown(f"### {company_name} ({ticker})")
        st.caption(f"{sector} · BSE {co.get('bse_code','—')}")
    with h2:
        if score:
            rating = score.get("rating_label", "—")
            st.metric("Composite Score", f"{score.get('composite_score', '—')}/100")
            st.markdown(_rating_badge(rating), unsafe_allow_html=True)
    with h3:
        completeness = score.get("data_completeness", 0) if score else 0
        st.metric("Data completeness", f"{completeness:.0f}%")

    st.divider()

    # ── scorecard + charts ────────────────────────────────────────────────
    left, right = st.columns([1, 1])

    with left:
        st.markdown("#### Parameter Scores")
        if score:
            for col_key, col_label in SCORE_COLS:
                v    = score.get(col_key) or 0
                rat  = score.get(col_key.replace(col_key.split("_")[0], col_key.split("_")[0]) + "_rationale",
                                  score.get(f"{col_key.split('_')[0]}_rationale", "—"))
                # find rationale key properly
                rat_key = col_key.replace(col_key, f"{col_key.split('_', 1)[0]}_rationale")
                # Map col_key → rationale key
                rat_map = {
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
                rat = score.get(rat_map.get(col_key, ""), "—")
                weight = WEIGHTS.get(col_key.split("_")[0], 0)
                with st.expander(f"{col_label}  ·  {v:.1f}/10  (weight {weight}%)", expanded=False):
                    st.markdown(_score_bar(v), unsafe_allow_html=True)
                    st.caption(SCORE_DESCRIPTIONS.get(col_key, ""))
                    st.write(rat or "Rationale not available.")
        else:
            st.info(f"No score for {ticker} {quarter}. Run scoring pipeline.")

        # Radar chart of all 10 scores
        if score:
            cats   = [lbl for _, lbl in SCORE_COLS]
            values = [score.get(k) or 0 for k, _ in SCORE_COLS]
            fig = go.Figure(go.Scatterpolar(
                r=values + [values[0]],
                theta=cats + [cats[0]],
                fill='toself',
                line_color='#378ADD',
                fillcolor='rgba(55,138,221,0.18)',
            ))
            fig.update_layout(
                polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
                showlegend=False,
                margin=dict(l=20, r=20, t=30, b=20),
                height=320,
            )
            st.plotly_chart(fig, use_container_width=True)

    with right:
        if fins:
            qs     = [f["quarter"] for f in fins][::-1]
            revs   = [f.get("revenue_cr") or 0 for f in fins][::-1]
            opms   = [f.get("opm_pct") or 0 for f in fins][::-1]
            pats   = [f.get("pat_cr") or 0 for f in fins][::-1]

            fig_rev = px.bar(x=qs, y=revs, title="Revenue (₹ Cr)",
                             color_discrete_sequence=["#378ADD"])
            fig_rev.update_layout(height=220, margin=dict(l=10, r=10, t=30, b=10),
                                  xaxis_title="", yaxis_title="₹ Cr")
            st.plotly_chart(fig_rev, use_container_width=True)

            fig_opm = px.line(x=qs, y=opms, title="EBITDA Margin %",
                              markers=True, color_discrete_sequence=["#1B8A4B"])
            fig_opm.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10),
                                   xaxis_title="", yaxis_title="%")
            st.plotly_chart(fig_opm, use_container_width=True)

            fig_pat = px.bar(x=qs, y=pats, title="PAT (₹ Cr)",
                             color_discrete_sequence=["#F5A623"])
            fig_pat.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10),
                                   xaxis_title="", yaxis_title="₹ Cr")
            st.plotly_chart(fig_pat, use_container_width=True)
        else:
            st.info("Financial data not yet loaded. Run Financials pipeline.")

    st.divider()

    # ── concall summary ───────────────────────────────────────────────────
    if concall:
        st.markdown("#### 📋 Concall Summary — " + quarter)
        c1, c2 = st.columns([3, 1])
        with c1:
            st.write(concall.get("ai_summary") or "Summary not available.")
            if concall.get("sector_outlook"):
                st.markdown("**Sector outlook:**")
                st.write(concall["sector_outlook"])
        with c2:
            pos  = concall.get("key_positives") or []
            risks= concall.get("key_risks")     or []
            if isinstance(pos,  str): pos  = json.loads(pos)  if pos  else []
            if isinstance(risks,str): risks= json.loads(risks) if risks else []
            if pos:
                st.markdown("**✅ Key positives**")
                for p in pos: st.markdown(f"- {p}")
            if risks:
                st.markdown("**⚠️ Key risks**")
                for r in risks: st.markdown(f"- {r}")
    else:
        st.info(f"No concall data for {ticker} {quarter}. Run Concall pipeline.")

    st.divider()

    # ── guidance history table ─────────────────────────────────────────────
    if guidance:
        st.markdown("#### 🎯 Walk-the-Talk — Guidance vs Actuals")
        g_rows = []
        for g in guidance:
            score_val = g.get("hit_miss_score")
            emoji = {2: "🟢 Major beat", 1: "✅ Minor beat / met",
                     0: "🟡 Met", -1: "🔴 Minor miss", -2: "❌ Major miss"
                     }.get(score_val, "⏳ Pending") if score_val is not None else "⏳ Pending"
            g_rows.append({
                "Guided in":       g.get("guidance_given_quarter"),
                "For period":      g.get("for_period"),
                "Metric":          g.get("metric"),
                "Type":            g.get("guidance_type"),
                "Guided direction":g.get("guided_direction"),
                "Guided magnitude":g.get("guided_magnitude"),
                "Guided value %":  g.get("guided_value_pct"),
                "Actual %":        g.get("actual_pct"),
                "Outcome":         emoji,
            })
        st.dataframe(pd.DataFrame(g_rows), use_container_width=True)
    else:
        st.info("Guidance history not yet populated. Run Concall pipeline then Scoring.")


# ── tab: sector view ──────────────────────────────────────────────────────────

def tab_sector():
    st.subheader("📊 Sector View")

    companies = db.get_all_companies()
    sectors   = sorted({c.get("sector", "—") for c in companies})
    if not sectors:
        st.info("No companies loaded.")
        return

    col1, col2 = st.columns([2, 2])
    with col1:
        sector  = st.selectbox("Select sector", sectors)
    with col2:
        quarters_opt = ["Q4FY26", "Q3FY26", "Q2FY26", "Q1FY26",
                         "Q4FY25", "Q3FY25", "Q2FY25", "Q1FY25"]
        active_q = db.get_config("active_quarter", "Q4FY26")
        quarter  = st.selectbox("Quarter", quarters_opt,
                                index=quarters_opt.index(active_q) if active_q in quarters_opt else 0)

    # All companies in sector
    sector_cos  = [c for c in companies if c.get("sector") == sector]
    ticker_list = [c["ticker"] for c in sector_cos]

    if not ticker_list:
        st.warning("No companies in this sector.")
        return

    # Scores for sector
    all_scores  = db.get_scores_leaderboard(quarter)
    sector_scores = [s for s in all_scores if s["ticker"] in ticker_list]

    if not sector_scores:
        st.info(f"No scores found for {sector} in {quarter}.")
        return

    # Summary metrics
    composites = [s.get("composite_score") or 0 for s in sector_scores]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Companies scored", len(sector_scores))
    m2.metric("Sector avg score", f"{sum(composites)/len(composites):.1f}")
    m3.metric("Highest", f"{max(composites):.1f}")
    m4.metric("Lowest",  f"{min(composites):.1f}")

    st.divider()

    # Ranked table for sector
    records = []
    for i, s in enumerate(sorted(sector_scores,
                                   key=lambda x: x.get("composite_score") or 0,
                                   reverse=True), 1):
        rec = {
            "Rank":    i,
            "Ticker":  s["ticker"],
            "Company": s.get("name","—")[:28],
            "Score":   s.get("composite_score"),
            "Rating":  s.get("rating_label","—"),
        }
        for col_key, col_label in SCORE_COLS:
            rec[col_label] = s.get(col_key)
        records.append(rec)

    df = pd.DataFrame(records)
    styled = df.style.applymap(_color_score,
                                subset=["Score"] + [lbl for _, lbl in SCORE_COLS])
    st.dataframe(styled, use_container_width=True)

    st.divider()

    # Score comparison bar chart
    st.markdown("#### Score comparison across sector")
    fig = px.bar(
        df.sort_values("Score"),
        x="Score", y="Ticker",
        orientation="h",
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

    # Walk-the-talk vs Revenue Growth scatter
    st.markdown("#### Walk-the-Talk vs Revenue Growth")
    scatter_data = [{
        "Ticker": s["ticker"],
        "Walk the Talk (S5)": s.get("s5_walk_the_talk") or 0,
        "Revenue Growth (S1)": s.get("s1_revenue_growth") or 0,
        "Score": s.get("composite_score") or 0,
    } for s in sector_scores]
    sdf = pd.DataFrame(scatter_data)
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


# ── tab: pipeline ─────────────────────────────────────────────────────────────

def tab_pipeline():
    st.subheader("⚙️ Data Pipeline")

    # Config display
    with st.expander("🔧 Config", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            active_q = db.get_config("active_quarter", "Q4FY26")
            new_q = st.text_input("Active quarter", value=active_q)
            if st.button("Update quarter"):
                db.set_config("active_quarter", new_q)
                st.success(f"Active quarter set to {new_q}")
        with c2:
            st.write("**Supabase project:** aiebaqvclyzxajigvkfd")
            st.write("**Region:** ap-south-1")
            st.write("**Tables:** re_companies, re_financials, re_concall,")
            st.write("         re_guidance, re_scores, re_technical")

    st.divider()
    st.markdown("#### Run Pipeline Steps")

    companies = db.get_all_companies()
    tickers   = [c["ticker"] for c in companies] if companies else []

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        if st.button("🌱 Seed companies", use_container_width=True):
            with st.spinner("Seeding company master..."):
                n = pl.seed_companies()
            st.success(f"Seeded {n} companies.")

    with col2:
        if st.button("📑 Fetch financials", use_container_width=True):
            progress = st.progress(0, text="Starting financial parser...")
            def _cb(i, total, ticker):
                progress.progress((i + 1) / total,
                                   text=f"Fetching {ticker} ({i+1}/{total})")
            with st.spinner("Fetching Screener.in data..."):
                r = pl.run_financials(tickers, progress_cb=_cb)
            progress.empty()
            st.success(f"✅ {len(r['processed'])} processed, {len(r['failed'])} failed, "
                       f"{r['records']} records inserted.")
            if r["failed"]:
                st.warning(f"Failed: {', '.join(r['failed'])}")

    with col3:
        if st.button("🎙️ Parse concalls", use_container_width=True):
            progress = st.progress(0, text="Starting concall parser...")
            def _cb(i, total, ticker):
                progress.progress((i + 1) / total,
                                   text=f"Parsing {ticker} ({i+1}/{total})")
            with st.spinner("Parsing concall PDFs via Claude AI..."):
                r = pl.run_concalls(tickers, progress_cb=_cb)
            progress.empty()
            st.success(f"✅ {len(r['processed'])} companies, "
                       f"{r.get('quarters_parsed',0)} quarters parsed.")
            if r["failed"]:
                st.warning(f"Failed: {', '.join(r['failed'])}")

    with col4:
        if st.button("📈 Update technicals", use_container_width=True):
            progress = st.progress(0, text="Starting Breeze fetch...")
            def _cb(i, total, ticker):
                progress.progress((i + 1) / total,
                                   text=f"Computing {ticker} ({i+1}/{total})")
            with st.spinner("Fetching Breeze API price data..."):
                r = pl.run_technicals(tickers, progress_cb=_cb)
            progress.empty()
            st.success(f"✅ {len(r['processed'])} processed, {r['records']} records.")
            if r["failed"]:
                st.warning(f"⚠️ Failed: {', '.join(r['failed'])}")

    with col5:
        if st.button("🧮 Run scoring", use_container_width=True, type="primary"):
            quarter  = db.get_config("active_quarter", "Q4FY26")
            progress = st.progress(0, text="Scoring...")
            def _cb(i, total, ticker):
                progress.progress((i + 1) / total,
                                   text=f"Scoring {ticker} ({i+1}/{total})")
            with st.spinner(f"Computing scores for {quarter}..."):
                r = pl.run_scoring(tickers, quarter, progress_cb=_cb)
            progress.empty()
            db.set_config("last_full_run",
                          __import__("datetime").datetime.now().strftime("%d %b %Y %H:%M"))
            st.success(f"✅ {len(r['processed'])} scored, {len(r['failed'])} failed.")

    st.divider()

    # Full pipeline
    st.markdown("#### 🚀 Full Pipeline (all steps in sequence)")
    if st.button("▶ Run full pipeline", type="primary", use_container_width=False):
        with st.status("Running full pipeline...", expanded=True) as status:
            def _cb(i, total, ticker):
                st.write(f"  → {ticker} ({i+1}/{total})")
            result = pl.run_full(progress_cb=_cb)
            db.set_config("last_full_run",
                          __import__("datetime").datetime.now().strftime("%d %b %Y %H:%M"))
            status.update(label="✅ Full pipeline complete!", state="complete")
        for step, r in result.items():
            if isinstance(r, dict) and "error" not in r:
                st.success(f"{step}: {r}")
            elif "error" in (r or {}):
                st.error(f"{step}: {r.get('error')}")

    st.divider()

    # Pipeline logs
    st.markdown("#### 📋 Recent Pipeline Logs")
    logs = db.get_recent_logs(20)
    if logs:
        log_rows = [{
            "Run type":  l.get("run_type"),
            "Status":    l.get("status"),
            "Processed": len(l.get("tickers_processed") or []),
            "Failed":    len(l.get("tickers_failed") or []),
            "Records":   l.get("records_inserted"),
            "Duration":  f"{l.get('duration_seconds','—')}s",
            "Started":   (l.get("started_at","")[:16].replace("T"," ")
                          if l.get("started_at") else "—"),
        } for l in logs]
        st.dataframe(pd.DataFrame(log_rows), use_container_width=True, height=300)
    else:
        st.info("No pipeline runs recorded yet.")

    # Companies table
    with st.expander("📋 Company master list", expanded=False):
        if companies:
            cos_df = pd.DataFrame([{
                "Ticker": c["ticker"],
                "Name":   c.get("name","—"),
                "Sector": c.get("sector","—"),
                "BSE":    c.get("bse_code","—"),
                "Index":  ", ".join(c.get("index_membership") or []),
            } for c in sorted(companies, key=lambda x: x["ticker"])])
            st.dataframe(cos_df, use_container_width=True)
        else:
            st.info("Run 🌱 Seed companies first.")


# ── main layout ───────────────────────────────────────────────────────────────

def main():
    st.markdown(
        "<h1 style='color:#042C53;margin-bottom:0'>📊 ResultsEdge</h1>"
        "<p style='color:#378ADD;margin-top:0;font-size:1.05rem'>"
        "NSE Earnings Intelligence Platform · Nifty 50 → 500</p>",
        unsafe_allow_html=True
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
