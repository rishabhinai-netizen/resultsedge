"""
concall_parser.py — Download concall PDFs via Screener.in document links.

Root cause of old failure: BSE filing API is geo-blocked from cloud servers
(returns empty Table[] for all queries). Fix: scrape Screener.in documents
section which lists per-quarter concall transcript links with BSE AttachHis
PDF URLs that ARE publicly accessible.
"""
import io
import json
import logging
import os
import re
import time
from datetime import date

import anthropic
import fitz           # PyMuPDF
import requests
import streamlit as st
from bs4 import BeautifulSoup

from modules import db

logger = logging.getLogger(__name__)

BSE_ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
SCREENER_BASE   = "https://www.screener.in/company"
REQUEST_TIMEOUT = 25
MAX_PDF_PAGES   = 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.screener.in/",
    "Accept-Language": "en-US,en;q=0.9",
}

# Month of concall announcement → (Q label, FY offset)
# Concall happens ~3-4 weeks after quarter end
# Apr/May/Jun concall = announcing Q4 (Jan-Mar)
# Jul/Aug/Sep concall = announcing Q1 (Apr-Jun)
# Oct/Nov/Dec concall = announcing Q2 (Jul-Sep)
# Jan/Feb/Mar concall = announcing Q3 (Oct-Dec)
_CONCALL_MONTH_MAP = {
    "Apr": ("Q4",  0), "May": ("Q4",  0), "Jun": ("Q4",  0),
    "Jul": ("Q1", +1), "Aug": ("Q1", +1), "Sep": ("Q1", +1),
    "Oct": ("Q2", +1), "Nov": ("Q2", +1), "Dec": ("Q2", +1),
    "Jan": ("Q3",  0), "Feb": ("Q3",  0), "Mar": ("Q3",  0),
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_anthropic_client() -> anthropic.Anthropic:
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        key = os.getenv("ANTHROPIC_API_KEY", "")
    return anthropic.Anthropic(api_key=key)


def _bulk_model() -> str:
    return db.get_config("claude_model_bulk", "claude-haiku-4-5-20251001")


def _month_to_quarter(month: str, year: int) -> str | None:
    """Convert concall announcement month/year to results quarter string."""
    if month not in _CONCALL_MONTH_MAP:
        return None
    q_label, fy_offset = _CONCALL_MONTH_MAP[month]
    fy = year + fy_offset
    return f"{q_label}FY{str(fy)[-2:]}"


def _next_quarter(q: str) -> str:
    m = re.match(r"Q([1-4])FY(\d{2})", q)
    if not m: return q
    n, fy = int(m.group(1)), int(m.group(2))
    return f"Q1FY{fy+1:02d}" if n == 4 else f"Q{n+1}FY{fy:02d}"


def _next_fy(q: str) -> str:
    m = re.match(r"Q[1-4]FY(\d{2})", q)
    if not m: return q
    return f"FY{int(m.group(1))+1:02d}"


# ── Screener.in document scraper ──────────────────────────────────────────────

def _get_screener_concall_links(ticker: str, session: requests.Session,
                                 max_quarters: int = 4) -> list[dict]:
    """
    Scrape the Screener.in company page documents section for concall transcripts.
    Returns list of {quarter, pdf_url, concall_date} dicts.
    """
    screener_ticker = {"MM": "M%26M", "BAJAJ-AUTO": "BAJAJ-AUTO"}.get(ticker, ticker)
    results = []

    for suffix in ["/consolidated/", "/"]:
        url = f"{SCREENER_BASE}/{screener_ticker}{suffix}"
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            docs = soup.find("section", id="documents")
            if not docs:
                continue

            seen_quarters = set()
            for li in docs.find_all("li"):
                text = li.get_text(" ", strip=True)
                links = li.find_all("a", href=True)
                for lnk in links:
                    href = lnk.get("href", "")
                    # Only concall transcript PDFs (AttachHis)
                    if "AttachHis" not in href:
                        continue
                    if "Transcript" not in text and "transcript" not in text.lower():
                        continue

                    # Extract month and year from text like "Apr 2026 Transcript ..."
                    mth_match = re.search(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
                        text
                    )
                    if not mth_match:
                        continue
                    month = mth_match.group(1)
                    year  = int(mth_match.group(2))
                    quarter = _month_to_quarter(month, year)
                    if not quarter or quarter in seen_quarters:
                        continue

                    # Build full PDF URL
                    if href.startswith("http"):
                        pdf_url = href
                    elif "AttachHis/" in href:
                        filename = href.split("AttachHis/")[-1]
                        pdf_url  = BSE_ATTACH_BASE + filename
                    else:
                        continue

                    seen_quarters.add(quarter)
                    results.append({
                        "quarter":      quarter,
                        "pdf_url":      pdf_url,
                        "concall_date": f"{year}-{list(_CONCALL_MONTH_MAP.keys()).index(month)+1:02d}-01",
                    })

                    if len(results) >= max_quarters:
                        break
                if len(results) >= max_quarters:
                    break

            if results:
                logger.info(f"{ticker}: found {len(results)} concall links from Screener.in")
                return results

        except Exception as e:
            logger.warning(f"{ticker}: Screener scrape error: {e}")

    logger.warning(f"{ticker}: no concall links found on Screener.in")
    return []


# ── PDF text extraction ───────────────────────────────────────────────────────

def _download_and_extract_text(pdf_url: str, session: requests.Session) -> str:
    r = session.get(pdf_url, timeout=REQUEST_TIMEOUT, stream=True)
    r.raise_for_status()
    doc  = fitz.open(stream=io.BytesIO(r.content), filetype="pdf")
    pages = min(len(doc), MAX_PDF_PAGES)
    text = "\n".join(doc[i].get_text("text") for i in range(pages))
    doc.close()
    text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    return text[:120_000]


# ── Claude AI guidance extraction ─────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a senior equity analyst extracting structured information from an Indian company earnings conference call transcript.

Company: {company_name} ({ticker}) | Quarter: {quarter}

Return ONLY a valid JSON object — no markdown, no explanation:

{{
  "guidance": {{
    "revenue": {{"given": true/false, "type": "quantified"/"directional", "direction": "growth"/"decline"/"stable", "magnitude": "strong"/"moderate"/"marginal"/null, "value_pct": null/number, "range_low_pct": null/number, "range_high_pct": null/number, "timeframe": "next_quarter"/"full_year"/"multi_year", "quote": "max 120 char verbatim quote or null"}},
    "margins": {{"given": true/false, "type": "quantified"/"directional", "direction": "expansion"/"compression"/"stable", "magnitude": "strong"/"moderate"/"marginal"/null, "bps_change": null/number, "target_pct": null/number, "timeframe": "next_quarter"/"full_year"/"multi_year", "quote": "max 120 char verbatim quote or null"}},
    "volume_or_units": {{"given": true/false, "direction": "growth"/"decline"/"stable", "magnitude": "strong"/"moderate"/"marginal"/null, "quote": "max 120 char verbatim quote or null"}},
    "capex": {{"given": true/false, "amount_cr": null/number, "direction": "increase"/"decrease"/"stable", "quote": "max 120 char verbatim quote or null"}}
  }},
  "sector_outlook": "2-3 sentences on management's sector/demand view",
  "key_positives": ["point 1", "point 2", "point 3"],
  "key_risks": ["risk 1", "risk 2", "risk 3"],
  "management_tone": "bullish"/"cautiously_optimistic"/"neutral"/"cautious"/"bearish",
  "confidence_level": "high"/"medium"/"low",
  "ai_summary": "3-4 sentence executive summary: results vs expectations, key guidance, outlook"
}}

Rules:
- Range guidance (15-20% growth): range_low_pct=15, range_high_pct=20, type=quantified
- "Double-digit growth": magnitude=moderate, direction=growth, type=directional
- "Strong double-digit growth": magnitude=strong, type=directional
- If not discussed: given=false, everything else null
- Return ONLY the JSON

TRANSCRIPT (first 80000 chars):
{transcript}"""


def _extract_with_claude(ticker: str, company_name: str,
                          quarter: str, transcript: str) -> dict:
    client = _get_anthropic_client()
    prompt = EXTRACTION_PROMPT.format(
        company_name=company_name, ticker=ticker,
        quarter=quarter, transcript=transcript[:80_000]
    )
    try:
        msg = client.messages.create(
            model=_bulk_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.error(f"{ticker} {quarter}: JSON parse error: {e}")
        return {}
    except Exception as e:
        logger.error(f"{ticker} {quarter}: Claude API error: {e}")
        return {}


# ── guidance rows builder ─────────────────────────────────────────────────────

def _build_guidance_rows(ticker: str, quarter: str, extracted: dict) -> list[dict]:
    rows = []
    guidance = extracted.get("guidance", {})
    for metric_key in ["revenue", "margins", "volume_or_units", "capex"]:
        g = guidance.get(metric_key, {})
        if not g or not g.get("given", False):
            continue
        timeframe = g.get("timeframe", "full_year")
        for_period = (_next_quarter(quarter) if timeframe == "next_quarter"
                      else _next_fy(quarter))
        rows.append({
            "ticker":                 ticker,
            "guidance_given_quarter": quarter,
            "for_period":             for_period,
            "metric":                 metric_key,
            "guidance_type":          g.get("type", "directional"),
            "guided_direction":       g.get("direction"),
            "guided_magnitude":       g.get("magnitude"),
            "guided_value_pct":       g.get("value_pct"),
            "guided_range_low_pct":   g.get("range_low_pct"),
            "guided_range_high_pct":  g.get("range_high_pct"),
            "guided_absolute_cr":     g.get("amount_cr"),
            "guided_quote":           g.get("quote"),
            "actual_value":           None,
            "actual_direction":       None,
            "actual_pct":             None,
            "hit_miss_score":         None,
            "hit_miss_label":         None,
        })
    return rows


# ── process one company ───────────────────────────────────────────────────────

def process_ticker(ticker: str, company_name: str,
                   target_quarters: list[str] | None = None,
                   max_quarters: int = 4,
                   force_refresh: bool = False) -> tuple[list[str], list[str]]:
    """
    Download and parse concalls for a company.
    SMART CACHE: If re_concall already has a complete record (ai_summary not null)
    for a quarter, it is SKIPPED — no PDF download, no Claude API call.
    Set force_refresh=True to re-parse even if record exists.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    filings = _get_screener_concall_links(ticker, session, max_quarters=max_quarters)
    if not filings:
        return [], [ticker]

    if target_quarters:
        filings = [f for f in filings if f["quarter"] in target_quarters]
    else:
        filings = filings[:max_quarters]

    processed, failed, skipped = [], [], []

    for filing in filings:
        q       = filing["quarter"]
        pdf_url = filing["pdf_url"]

        # ── SMART CACHE CHECK ──────────────────────────────────────────────
        if not force_refresh:
            existing = db.get_concall(ticker, q)
            if existing and existing.get("ai_summary"):
                logger.info(f"{ticker} {q}: already parsed ({existing.get('parsed_at','?')[:10]}), skipping.")
                skipped.append(q)
                processed.append(q)   # count as processed (data is there)
                continue
        # ──────────────────────────────────────────────────────────────────

        try:
            transcript = _download_and_extract_text(pdf_url, session)
            if len(transcript) < 200:
                logger.warning(f"{ticker} {q}: transcript too short ({len(transcript)} chars)")
                failed.append(q)
                continue

            extracted = _extract_with_claude(ticker, company_name, q, transcript)
            if not extracted:
                failed.append(q)
                continue

            concall_row = {
                "ticker":          ticker,
                "quarter":         q,
                "concall_date":    filing.get("concall_date"),
                "pdf_url":         pdf_url,
                "raw_text":        transcript[:50_000],
                "ai_summary":      extracted.get("ai_summary"),
                "guidance_json":   json.dumps(extracted.get("guidance", {})),
                "sector_outlook":  extracted.get("sector_outlook"),
                "key_positives":   json.dumps(extracted.get("key_positives", [])),
                "key_risks":       json.dumps(extracted.get("key_risks", [])),
                "mgmt_tone":       extracted.get("management_tone"),
                "confidence_level":extracted.get("confidence_level"),
                "parsed_at":       __import__("datetime").datetime.utcnow().isoformat(),
                "parse_model":     _bulk_model(),
            }
            db.upsert_concall(concall_row)
            guidance_rows = _build_guidance_rows(ticker, q, extracted)
            if guidance_rows:
                db.upsert_guidance(guidance_rows)

            processed.append(q)
            logger.info(f"{ticker} {q}: parsed OK — {len(guidance_rows)} guidance rows")
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"{ticker} {q}: {e}")
            failed.append(q)

    if skipped:
        logger.info(f"{ticker}: {len(skipped)} quarters skipped (already in DB)")

    return processed, failed


# ── batch runner ──────────────────────────────────────────────────────────────

def run_for_companies(companies: list[dict],
                      target_quarters: list[str] | None = None,
                      max_quarters: int = 4,
                      force_refresh: bool = False,
                      progress_cb=None) -> tuple[list[str], list[str], int]:
    processed_all, failed_all, total = [], [], 0
    for i, co in enumerate(companies):
        ticker = co["ticker"]
        name   = co.get("name", ticker)
        if progress_cb:
            progress_cb(i, len(companies), ticker)
        try:
            p, f = process_ticker(ticker, name, target_quarters,
                                   max_quarters, force_refresh)
            if p:
                processed_all.append(ticker)
                total += len(p)
            else:
                failed_all.append(ticker)
        except Exception as e:
            logger.error(f"{ticker}: {e}")
            failed_all.append(ticker)
        time.sleep(1.0)
    return processed_all, failed_all, total
