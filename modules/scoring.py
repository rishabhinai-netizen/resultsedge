"""
scoring.py — 10-parameter scoring engine for ResultsEdge.

Parameters (weights):
  S1  Revenue growth quality     10%
  S2  Profitability expansion     10%
  S3  Cash flow quality           5%
  S4  Guidance quality           10%
  S5  Walk the talk (historical) 20%
  S6  Current quarter delivery   10%
  S7  Sector position            10%
  S8  Sector opportunity          5%
  S9  RS & momentum              10%
  S10 Valuation context          10%
  ─────────────────────────────────
  Composite (0–100)             100%
"""
import logging
from typing import Any

from modules import db

logger = logging.getLogger(__name__)

WEIGHTS = {
    "s1": 0.10, "s2": 0.10, "s3": 0.05,
    "s4": 0.10, "s5": 0.20, "s6": 0.10,
    "s7": 0.10, "s8": 0.05, "s9": 0.10, "s10": 0.10,
}

RATING_LABELS = [(85, "Excellent"), (70, "Strong"),
                 (55, "Average"),   (40, "Weak"), (0, "Poor")]


def _rating(score: float) -> str:
    for threshold, label in RATING_LABELS:
        if score >= threshold:
            return label
    return "Poor"


def _safe(val, default=0.0):
    return float(val) if val is not None else default


# ─────────────────────────────────────────────────────────────────────────────
# S1 — Revenue growth quality
# ─────────────────────────────────────────────────────────────────────────────

def score_s1(financials: list[dict]) -> tuple[float, str]:
    if not financials:
        return 4.0, "No financial data available."

    latest  = financials[0]
    yoy     = _safe(latest.get("revenue_yoy"), 0)

    # 4-quarter growth trend
    if len(financials) >= 4:
        yoys = [_safe(f.get("revenue_yoy"), 0) for f in financials[:4]]
        if yoys[0] > yoys[1] > yoys[2]:
            trend = "accelerating"
        elif yoys[0] < yoys[1] < yoys[2]:
            trend = "decelerating"
        else:
            trend = "stable"
    else:
        trend = "stable"

    if yoy > 30:    base = 10
    elif yoy > 25:  base = 9
    elif yoy > 20:  base = 8
    elif yoy > 15:  base = 7
    elif yoy > 10:  base = 6
    elif yoy > 5:   base = 5
    elif yoy > 0:   base = 4
    elif yoy > -5:  base = 3
    elif yoy > -10: base = 2
    else:           base = 1

    if trend == "accelerating":   base = min(10, base + 1)
    elif trend == "decelerating": base = max(1, base - 1)

    return float(base), f"YoY rev growth: {yoy:+.1f}% ({trend} trend over 4Q)."


# ─────────────────────────────────────────────────────────────────────────────
# S2 — Profitability expansion
# ─────────────────────────────────────────────────────────────────────────────

def score_s2(financials: list[dict]) -> tuple[float, str]:
    if not financials:
        return 4.0, "No financial data."
    latest  = financials[0]
    pat_yoy = _safe(latest.get("pat_yoy"), 0)
    curr_opm= _safe(latest.get("opm_pct"), 0)

    # OPM change vs 4-quarter average (exclude latest)
    if len(financials) >= 5:
        hist_opms = [_safe(f.get("opm_pct"), 0) for f in financials[1:5]]
        avg_opm   = sum(hist_opms) / len(hist_opms)
        opm_chg   = (curr_opm - avg_opm) * 100   # in basis points
    else:
        opm_chg = 0
        avg_opm = curr_opm

    # OPM score
    if opm_chg > 300:   opm_s = 10
    elif opm_chg > 200: opm_s = 9
    elif opm_chg > 100: opm_s = 8
    elif opm_chg > 50:  opm_s = 7
    elif opm_chg > 0:   opm_s = 6
    elif opm_chg > -50: opm_s = 5
    elif opm_chg > -100:opm_s = 4
    elif opm_chg > -200:opm_s = 3
    else:               opm_s = 2

    # PAT score
    if pat_yoy > 30:   pat_s = 10
    elif pat_yoy > 20: pat_s = 8
    elif pat_yoy > 10: pat_s = 7
    elif pat_yoy > 0:  pat_s = 5
    elif pat_yoy > -10:pat_s = 3
    else:              pat_s = 1

    score = round(0.60 * opm_s + 0.40 * pat_s, 1)
    return score, (f"OPM {curr_opm:.1f}% ({opm_chg:+.0f}bps vs 4Q avg). "
                   f"PAT YoY: {pat_yoy:+.1f}%.")


# ─────────────────────────────────────────────────────────────────────────────
# S3 — Cash flow quality
# ─────────────────────────────────────────────────────────────────────────────

def score_s3(financials: list[dict]) -> tuple[float, str]:
    latest = financials[0] if financials else {}
    cfo    = latest.get("cfo_cr")
    pat    = latest.get("pat_cr")

    if cfo is None or pat is None or _safe(pat) == 0:
        return 5.0, "CFO data not available (Screener.in quarterly). Score defaulted to 5."

    ratio = _safe(cfo) / _safe(pat)
    if ratio > 1.5:   s = 10
    elif ratio > 1.2: s = 9
    elif ratio > 1.0: s = 8
    elif ratio > 0.8: s = 6
    elif ratio > 0.6: s = 4
    elif ratio > 0.4: s = 3
    else:             s = 2

    return float(s), f"CFO/PAT ratio: {ratio:.2f}x (₹{_safe(cfo):.0f}Cr / ₹{_safe(pat):.0f}Cr)."


# ─────────────────────────────────────────────────────────────────────────────
# S4 — Guidance quality (current quarter's concall)
# ─────────────────────────────────────────────────────────────────────────────

def score_s4(concall: dict | None) -> tuple[float, str]:
    if not concall:
        return 2.0, "No concall data found."

    g = concall.get("guidance_json") or {}
    if isinstance(g, str):
        import json as _j
        try: g = _j.loads(g)
        except: g = {}

    metrics_given  = 0
    quantified     = 0
    for metric in ["revenue", "margins", "volume_or_units", "capex"]:
        m = g.get(metric, {})
        if m.get("given", False):
            metrics_given += 1
            if m.get("type") == "quantified":
                quantified += 1

    if quantified >= 3:                       s = 10
    elif quantified >= 2:                     s = 9
    elif quantified >= 1 and metrics_given >= 3: s = 8
    elif quantified >= 1 and metrics_given >= 2: s = 7
    elif metrics_given >= 3:                  s = 6
    elif metrics_given >= 2:                  s = 5
    elif metrics_given >= 1:                  s = 4
    else:                                     s = 2

    return float(s), f"{metrics_given} metrics guided, {quantified} quantified."


# ─────────────────────────────────────────────────────────────────────────────
# S5 — Walk the talk (historical, last 4 quarters)
# ─────────────────────────────────────────────────────────────────────────────

def score_s5(guidance_history: list[dict]) -> tuple[float, str]:
    scored = [g for g in guidance_history if g.get("hit_miss_score") is not None]
    if not scored:
        return 5.0, "Walk-the-talk data not yet populated (run guidance-linking)."

    # Take up to 8 most recent scored guidance records (up to 4 quarters × 2 metrics)
    scored = sorted(scored, key=lambda g: (g.get("guidance_given_quarter",""), g.get("metric","")),
                    reverse=True)[:8]
    avg = sum(_safe(g["hit_miss_score"]) for g in scored) / len(scored)

    # avg in [-2, +2] → normalize to [1, 10]
    score = round(((avg + 2) / 4) * 9 + 1, 1)
    score = max(1.0, min(10.0, score))

    beats  = sum(1 for g in scored if _safe(g.get("hit_miss_score")) > 0)
    misses = sum(1 for g in scored if _safe(g.get("hit_miss_score")) < 0)
    return score, (f"{beats}/{len(scored)} guidance points beat. "
                   f"{misses} missed. Avg hit score: {avg:+.2f}.")


# ─────────────────────────────────────────────────────────────────────────────
# S6 — Current quarter delivery (most recent quarter vs prior guidance)
# ─────────────────────────────────────────────────────────────────────────────

def score_s6(guidance_history: list[dict], active_quarter: str) -> tuple[float, str]:
    """
    Find guidance records where for_period == active_quarter and scored.
    """
    current = [g for g in guidance_history
               if g.get("for_period") == active_quarter
               and g.get("hit_miss_score") is not None]

    if not current:
        return 5.0, f"No scored guidance found targeting {active_quarter}."

    avg = sum(_safe(g["hit_miss_score"]) for g in current) / len(current)
    score_map = {-2: 1.0, -1: 3.0, 0: 6.0, 1: 8.0, 2: 10.0}
    # Use avg rounded to nearest int
    s = score_map.get(round(avg), 5.0)

    labels = [g.get("hit_miss_label","—") for g in current]
    return s, f"Current quarter delivery ({active_quarter}): {', '.join(set(labels))}. Avg score: {avg:+.1f}."


# ─────────────────────────────────────────────────────────────────────────────
# S7 — Sector position (relative rank among peers)
# ─────────────────────────────────────────────────────────────────────────────

def score_s7(ticker: str, sector_financials: list[dict]) -> tuple[float, str]:
    if len(sector_financials) < 2:
        return 5.0, "Insufficient peer data for sector ranking."

    ticker_fin = next((f for f in sector_financials if f["ticker"] == ticker), None)
    if not ticker_fin:
        return 5.0, "Ticker not found in sector data."

    n = len(sector_financials)

    # Revenue growth rank (descending — higher growth = better rank)
    rev_growths = sorted(sector_financials,
                         key=lambda f: _safe(f.get("revenue_yoy"), 0),
                         reverse=True)
    rev_rank = next((i + 1 for i, f in enumerate(rev_growths) if f["ticker"] == ticker), n)

    # Margin rank (descending)
    margins = sorted(sector_financials,
                     key=lambda f: _safe(f.get("opm_pct"), 0),
                     reverse=True)
    mar_rank = next((i + 1 for i, f in enumerate(margins) if f["ticker"] == ticker), n)

    avg_rank_pct = ((rev_rank / n) + (mar_rank / n)) / 2   # 0 = best, 1 = worst

    if avg_rank_pct <= 0.10:   s = 10
    elif avg_rank_pct <= 0.20: s = 9
    elif avg_rank_pct <= 0.30: s = 8
    elif avg_rank_pct <= 0.40: s = 7
    elif avg_rank_pct <= 0.50: s = 6
    elif avg_rank_pct <= 0.60: s = 5
    elif avg_rank_pct <= 0.70: s = 4
    elif avg_rank_pct <= 0.80: s = 3
    else:                       s = 2

    return float(s), (f"Rev growth rank: {rev_rank}/{n}; "
                      f"OPM rank: {mar_rank}/{n} in sector ({n} peers).")


# ─────────────────────────────────────────────────────────────────────────────
# S8 — Sector opportunity
# ─────────────────────────────────────────────────────────────────────────────

def score_s8(concall: dict | None) -> tuple[float, str]:
    if not concall:
        return 5.0, "No concall data for sector opportunity."

    tone    = concall.get("mgmt_tone", "neutral") or "neutral"
    pos     = concall.get("key_positives")  or []
    risks   = concall.get("key_risks")      or []
    if isinstance(pos, str):
        import json as _j
        try: pos = _j.loads(pos)
        except: pos = []
    if isinstance(risks, str):
        import json as _j
        try: risks = _j.loads(risks)
        except: risks = []

    tone_score = {
        "bullish": 9,
        "cautiously_optimistic": 7,
        "neutral": 5,
        "cautious": 3,
        "bearish": 2,
    }.get(tone, 5)

    net_sentiment = len(pos) - len(risks)
    if net_sentiment >= 2:   tone_score = min(10, tone_score + 1)
    elif net_sentiment <= -2: tone_score = max(1, tone_score - 1)

    return float(tone_score), (f"Mgmt tone: {tone}. "
                                f"{len(pos)} positives, {len(risks)} risks mentioned.")


# ─────────────────────────────────────────────────────────────────────────────
# S9 — RS & Momentum
# ─────────────────────────────────────────────────────────────────────────────

def score_s9(technical: dict | None) -> tuple[float, str]:
    if not technical:
        return 5.0, "No technical data available."

    rs    = _safe(technical.get("rs_63d"), 100)
    v200  = _safe(technical.get("pct_vs_200dma"), 0)
    rk    = _safe(technical.get("rank_52w"), 50)

    def _rs_s(v):
        if v > 150: return 10
        if v > 130: return 9
        if v > 115: return 8
        if v > 105: return 7
        if v > 100: return 6
        if v > 90:  return 5
        if v > 75:  return 4
        if v > 60:  return 3
        return 2

    def _ma_s(v):
        if v > 30:  return 10
        if v > 20:  return 9
        if v > 10:  return 7
        if v > 5:   return 6
        if v >= 0:  return 5
        if v > -10: return 3
        return 2

    def _rk_s(v):
        if v > 90: return 10
        if v > 75: return 8
        if v > 60: return 7
        if v > 50: return 6
        if v > 35: return 5
        if v > 25: return 4
        return 2

    score = round(0.50 * _rs_s(rs) + 0.30 * _ma_s(v200) + 0.20 * _rk_s(rk), 1)
    return score, (f"RS-63d: {rs:.0f}. "
                   f"vs 200DMA: {v200:+.1f}%. "
                   f"52W rank: {rk:.0f}%.")


# ─────────────────────────────────────────────────────────────────────────────
# S10 — Valuation context
# ─────────────────────────────────────────────────────────────────────────────

def score_s10(financials: list[dict], sector_pe_median: float | None = None) -> tuple[float, str]:
    # Quarterly EPS * 4 ≈ annualised; P/E needs price data which we get from Breeze.
    # For now, we score based on EPS growth trajectory vs sector.
    if not financials:
        return 5.0, "No valuation data."

    latest  = financials[0]
    eps_now = _safe(latest.get("eps"), 0)
    if len(financials) >= 5:
        eps_4yago = _safe(financials[4].get("eps"), 0)
        eps_yoy = ((eps_now - eps_4yago) / abs(eps_4yago) * 100
                   if eps_4yago != 0 else 0)
    else:
        eps_yoy = 0

    # Simple growth-quality proxy (more sophisticated P/E data can be added later)
    if eps_yoy > 30:   s = 9
    elif eps_yoy > 20: s = 8
    elif eps_yoy > 15: s = 7
    elif eps_yoy > 10: s = 6
    elif eps_yoy > 5:  s = 5
    elif eps_yoy > 0:  s = 4
    elif eps_yoy > -5: s = 3
    else:              s = 2

    note = ""
    if sector_pe_median:
        note = f" Sector median P/E: {sector_pe_median:.1f}x."

    return float(s), f"EPS YoY (4Q): {eps_yoy:+.1f}%.{note} (Full P/E ranking in Phase 2.)"


# ─────────────────────────────────────────────────────────────────────────────
# Walk-the-talk guidance linking
# ─────────────────────────────────────────────────────────────────────────────

def _direction_from_pct(pct: float) -> str:
    if pct > 2:   return "growth"
    if pct < -2:  return "decline"
    return "stable"


def _hit_miss(guided_direction: str, guided_magnitude: str | None,
               guided_value_pct: float | None, guided_range_low: float | None,
               guided_range_high: float | None, actual_pct: float) -> tuple[int, str]:
    """
    Core walk-the-talk scoring logic.
    Returns (score: -2 to +2, label).

    If quantified guidance:  compare actual to range/value
    If directional guidance: match direction + magnitude
    """
    actual_dir = _direction_from_pct(actual_pct)

    # ── quantified guidance ───────────────────────────────────────────────────
    if guided_value_pct is not None:
        target = guided_value_pct
        pct_diff = ((actual_pct - target) / abs(target) * 100) if target != 0 else 0
        if pct_diff > 10:   return 2, "major_beat"
        if pct_diff > 3:    return 1, "minor_beat"
        if pct_diff > -3:   return 1, "met"           # within 3% = delivered
        if pct_diff > -10:  return -1, "minor_miss"
        return -2, "major_miss"

    if guided_range_low is not None and guided_range_high is not None:
        if actual_pct >= guided_range_low and actual_pct <= guided_range_high:
            return 1, "met"
        if actual_pct > guided_range_high:
            excess = (actual_pct - guided_range_high) / guided_range_high * 100
            return (2, "major_beat") if excess > 10 else (1, "minor_beat")
        else:
            deficit = (guided_range_low - actual_pct) / guided_range_low * 100
            return (-2, "major_miss") if deficit > 10 else (-1, "minor_miss")

    # ── directional guidance ──────────────────────────────────────────────────
    mag = guided_magnitude or "moderate"
    mag_threshold = {"strong": 15.0, "moderate": 8.0, "marginal": 3.0}.get(mag, 8.0)

    if guided_direction == "growth":
        if actual_dir == "growth":
            if actual_pct >= mag_threshold * 1.2:  return 2, "major_beat"
            if actual_pct >= mag_threshold * 0.7:  return 1, "met"
            if actual_pct > 0:                     return -1, "minor_miss"
            return -2, "major_miss"
        else:
            return -2, "major_miss"

    if guided_direction == "decline":
        if actual_dir == "decline":
            if actual_pct <= -(mag_threshold * 0.7): return 1, "met"
            return -1, "minor_miss"
        return -1, "minor_miss"

    # stable
    if abs(actual_pct) <= 5:  return 1, "met"
    if abs(actual_pct) <= 10: return -1, "minor_miss"
    return -2, "major_miss"


def link_guidance_to_actuals(ticker: str, active_quarter: str) -> int:
    """
    For every guidance row targeting *active_quarter*,
    look up actual financials and compute hit_miss_score.
    Returns number of guidance rows updated.
    """
    guidance_rows = [g for g in db.get_guidance_history(ticker, limit=40)
                     if g.get("for_period") == active_quarter
                     and g.get("hit_miss_score") is None]

    if not guidance_rows:
        return 0

    financials = db.get_financials(ticker, limit=8)
    fin_q = next((f for f in financials if f["quarter"] == active_quarter), None)
    if not fin_q:
        return 0

    updated = 0
    for g in guidance_rows:
        metric = g.get("metric")
        actual_pct = None

        if metric == "revenue":
            actual_pct = fin_q.get("revenue_yoy")
        elif metric == "margins":
            curr_opm = fin_q.get("opm_pct")
            if curr_opm and financials:
                # Find the same quarter's OPM from prior year (4 rows back)
                same_q_last_yr = [f for f in financials if f["quarter"] != active_quarter
                                   and f.get("opm_pct")]
                if len(same_q_last_yr) >= 4:
                    prior_opm = same_q_last_yr[3].get("opm_pct")
                    if prior_opm and prior_opm != 0:
                        actual_pct = (curr_opm - prior_opm) / abs(prior_opm) * 100
        elif metric == "volume_or_units":
            actual_pct = fin_q.get("revenue_yoy")  # proxy if no volume data
        elif metric == "capex":
            actual_pct = None  # capex not in quarterly table; skip

        if actual_pct is None:
            continue

        score, label = _hit_miss(
            guided_direction  = g.get("guided_direction", "growth"),
            guided_magnitude  = g.get("guided_magnitude"),
            guided_value_pct  = g.get("guided_value_pct"),
            guided_range_low  = g.get("guided_range_low_pct"),
            guided_range_high = g.get("guided_range_high_pct"),
            actual_pct        = actual_pct,
        )
        db.update_guidance_actuals(
            ticker=ticker,
            quarter=active_quarter,
            metric=metric,
            actual_pct=actual_pct,
            actual_direction=_direction_from_pct(actual_pct),
            hit_miss_score=score,
            hit_miss_label=label,
        )
        updated += 1

    return updated


# ─────────────────────────────────────────────────────────────────────────────
# Composite scorer — entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_composite_score(ticker: str, quarter: str,
                             sector_peers_financials: list[dict] | None = None) -> dict:
    """
    Compute all 10 scores and composite for a ticker/quarter.
    Stores result in re_scores and returns the score dict.
    """
    # Load all data
    financials       = db.get_financials(ticker, limit=8)
    concall          = db.get_concall(ticker, quarter)
    guidance_history = db.get_guidance_history(ticker, limit=40)
    technical        = db.get_latest_technical(ticker)
    active_q         = quarter

    # First run guidance linking for this quarter
    link_guidance_to_actuals(ticker, active_q)
    # Re-fetch after linking
    guidance_history = db.get_guidance_history(ticker, limit=40)

    # Sector peers (for S7)
    sector_peers = sector_peers_financials or []

    s1, r1 = score_s1(financials)
    s2, r2 = score_s2(financials)
    s3, r3 = score_s3(financials)
    s4, r4 = score_s4(concall)
    s5, r5 = score_s5(guidance_history)
    s6, r6 = score_s6(guidance_history, active_q)
    s7, r7 = score_s7(ticker, sector_peers if sector_peers else financials)
    s8, r8 = score_s8(concall)
    s9, r9 = score_s9(technical)
    s10,r10= score_s10(financials)

    composite = round(
        (s1  * WEIGHTS["s1"]  + s2  * WEIGHTS["s2"]  + s3  * WEIGHTS["s3"] +
         s4  * WEIGHTS["s4"]  + s5  * WEIGHTS["s5"]  + s6  * WEIGHTS["s6"] +
         s7  * WEIGHTS["s7"]  + s8  * WEIGHTS["s8"]  + s9  * WEIGHTS["s9"] +
         s10 * WEIGHTS["s10"]) * 10,
        1
    )

    data_fields = [financials, concall, guidance_history, technical]
    available   = sum(1 for d in data_fields if d)
    completeness= round(available / len(data_fields) * 100, 1)

    row = {
        "ticker":                ticker,
        "quarter":               quarter,
        "s1_revenue_growth":     s1,
        "s2_profitability":      s2,
        "s3_cashflow":           s3,
        "s4_guidance_quality":   s4,
        "s5_walk_the_talk":      s5,
        "s6_current_delivery":   s6,
        "s7_sector_position":    s7,
        "s8_sector_opportunity": s8,
        "s9_momentum":           s9,
        "s10_valuation":         s10,
        "composite_score":       composite,
        "rating_label":          _rating(composite),
        "s1_rationale":          r1,
        "s2_rationale":          r2,
        "s3_rationale":          r3,
        "s4_rationale":          r4,
        "s5_rationale":          r5,
        "s6_rationale":          r6,
        "s7_rationale":          r7,
        "s8_rationale":          r8,
        "s9_rationale":          r9,
        "s10_rationale":         r10,
        "data_completeness":     completeness,
    }
    db.upsert_score(row)
    logger.info(f"{ticker} {quarter}: composite={composite} ({_rating(composite)}), "
                f"completeness={completeness}%")
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Batch scorer
# ─────────────────────────────────────────────────────────────────────────────

def run_scoring_for_all(tickers: list[str], quarter: str,
                         progress_cb=None) -> tuple[list[str], list[str]]:
    """Score all tickers for a given quarter."""
    # Pre-load latest financials for all tickers to enable sector ranking
    all_latest = db.get_latest_financials_all(quarter)

    processed, failed = [], []
    for i, ticker in enumerate(tickers):
        if progress_cb:
            progress_cb(i, len(tickers), ticker)
        try:
            # Get this company's sector from companies table
            cos = db.get_all_companies()
            co  = next((c for c in cos if c["ticker"] == ticker), {})
            sector = co.get("sector", "")
            sector_peers = [f for f in all_latest
                            if f["ticker"] != ticker] if not sector else \
                           [f for f in all_latest
                            if _get_sector(f["ticker"], cos) == sector
                            and f["ticker"] != ticker]

            compute_composite_score(ticker, quarter, sector_peers)
            processed.append(ticker)
        except Exception as e:
            logger.error(f"{ticker}: scoring error: {e}")
            failed.append(ticker)

    return processed, failed


def _get_sector(ticker: str, companies: list[dict]) -> str:
    co = next((c for c in companies if c["ticker"] == ticker), {})
    return co.get("sector", "")
