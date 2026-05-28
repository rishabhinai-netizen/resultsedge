"""
concall_parser.py — Download concall PDFs from BSE, parse with Claude AI.
Extracts: revenue/margin/volume guidance (direction + magnitude + quotes),
management tone, sector outlook, key positives & risks.
Stores in re_concall and re_guidance via db.py.
"""
import io
import json
import logging
import os
import time
from datetime import datetime, date

import anthropic
import fitz                          # PyMuPDF
import requests
import streamlit as st

from modules import db

logger = logging.getLogger(__name__)

BSE_FILING_API = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?strCat=Concall&strType=C&strScrip={bse_code}"
    "&strSearch=P&strToDate=&strPrevDate=&subcategory=-1"
)
BSE_PDF_BASE   = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
NSE_FILING_API = (
    "https://www.nseindia.com/api/corp-filing-data"
    "?index=equities&symbol={ticker}&category=Concall&issuer=&fromDate=&toDate="
)
HEADERS_BSE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
}
HEADERS_NSE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
    "Accept": "application/json, text/plain, */*",
}
MAX_PDF_PAGES   = 60
REQUEST_TIMEOUT = 30


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_anthropic_client() -> anthropic.Anthropic:
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        key = os.getenv("ANTHROPIC_API_KEY", "")
    return anthropic.Anthropic(api_key=key)


def _bulk_model() -> str:
    return db.get_config("claude_model_bulk", "claude-haiku-4-5-20251001")


def _date_from_str(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _quarter_from_date(d: date) -> str:
    """Infer NSE quarter string from a date."""
    if 4 <= d.month <= 6:   q, fy = "Q1", d.year + 1
    elif 7 <= d.month <= 9:  q, fy = "Q2", d.year + 1
    elif 10 <= d.month <= 12:q, fy = "Q3", d.year + 1
    else:                     q, fy = "Q4", d.year
    return f"{q}FY{str(fy)[-2:]}"


# ── BSE filing discovery ──────────────────────────────────────────────────────

def _get_bse_concall_filings(bse_code: str) -> list[dict]:
    """Return list of {date, pdf_url, quarter} from BSE concall filings."""
    url = BSE_FILING_API.format(bse_code=bse_code)
    try:
        resp = requests.get(url, headers=HEADERS_BSE, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"BSE filing API error for {bse_code}: {e}")
        return []

    filings = []
    for row in data.get("Table", []):
        attachment = row.get("ATTACHMENTNAME", "").strip()
        tdtime     = row.get("TDTIME", "").strip()
        if not attachment:
            continue
        pdf_url = BSE_PDF_BASE + attachment
        d       = _date_from_str(tdtime)
        quarter = _quarter_from_date(d) if d else None
        if quarter:
            filings.append({"date": d, "pdf_url": pdf_url, "quarter": quarter})

    # deduplicate by quarter (keep earliest filing for that quarter)
    seen: dict[str, dict] = {}
    for f in filings:
        q = f["quarter"]
        if q not in seen or f["date"] < seen[q]["date"]:
            seen[q] = f
    return list(seen.values())


def _get_nse_concall_filings(ticker: str) -> list[dict]:
    """Fallback: NSE filing search for concall PDFs."""
    url = NSE_FILING_API.format(ticker=ticker)
    session = requests.Session()
    # NSE requires a session cookie obtained from the main page
    try:
        session.get("https://www.nseindia.com", headers=HEADERS_NSE,
                    timeout=REQUEST_TIMEOUT)
        resp = session.get(url, headers=HEADERS_NSE, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"NSE filing API error for {ticker}: {e}")
        return []

    filings = []
    for row in data.get("data", []):
        pdf_url = row.get("attachmentLink", "") or row.get("pdfUrl", "")
        d_str   = row.get("date", "") or row.get("broadcastDate", "")
        if not pdf_url:
            continue
        d       = _date_from_str(d_str)
        quarter = _quarter_from_date(d) if d else None
        if quarter:
            filings.append({"date": d, "pdf_url": pdf_url, "quarter": quarter})
    return filings


# ── PDF text extraction ───────────────────────────────────────────────────────

def _download_and_extract_text(pdf_url: str, max_pages: int = MAX_PDF_PAGES) -> str:
    """Download PDF and extract text with PyMuPDF."""
    try:
        resp = requests.get(pdf_url, headers=HEADERS_BSE,
                            timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
        pdf_bytes = resp.content
    except Exception as e:
        raise RuntimeError(f"PDF download failed: {e}")

    try:
        doc   = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
        pages = min(len(doc), max_pages)
        texts = [doc[i].get_text("text") for i in range(pages)]
        doc.close()
        full_text = "\n".join(texts)
        # Normalise whitespace
        full_text = "\n".join(line.strip() for line in full_text.splitlines() if line.strip())
        return full_text[:120_000]   # cap at ~120K chars to stay within Claude context
    except Exception as e:
        raise RuntimeError(f"PDF text extraction failed: {e}")


# ── Claude AI guidance extraction ─────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a senior equity analyst extracting structured information from an Indian company's quarterly earnings conference call transcript.

Company: {company_name} ({ticker}) | Quarter: {quarter}

Extract EXACTLY the following and return ONLY a valid JSON object — no markdown, no preamble, no explanation:

{{
  "guidance": {{
    "revenue": {{
      "given": true/false,
      "type": "quantified" or "directional",
      "direction": "growth" | "decline" | "stable",
      "magnitude": "strong" | "moderate" | "marginal" | null,
      "value_pct": null or number (e.g. 15.0 for 15% guidance),
      "range_low_pct": null or number,
      "range_high_pct": null or number,
      "absolute_cr": null or number,
      "timeframe": "next_quarter" | "full_year" | "multi_year",
      "quote": "exact management quote max 120 chars or null"
    }},
    "margins": {{
      "given": true/false,
      "type": "quantified" or "directional",
      "direction": "expansion" | "compression" | "stable",
      "magnitude": "strong" | "moderate" | "marginal" | null,
      "bps_change": null or number,
      "target_pct": null or number,
      "timeframe": "next_quarter" | "full_year" | "multi_year",
      "quote": "exact management quote max 120 chars or null"
    }},
    "volume_or_units": {{
      "given": true/false,
      "direction": "growth" | "decline" | "stable",
      "magnitude": "strong" | "moderate" | "marginal" | null,
      "value": null or number,
      "unit": null or "units" or "vehicles" or "tonnes" or other,
      "quote": "exact management quote max 120 chars or null"
    }},
    "capex": {{
      "given": true/false,
      "amount_cr": null or number,
      "direction": "increase" | "decrease" | "stable",
      "quote": "exact management quote max 120 chars or null"
    }}
  }},
  "sector_outlook": "2-3 sentences on management's view of industry/sector demand and competitive dynamics",
  "key_positives": ["point 1", "point 2", "point 3"],
  "key_risks": ["risk 1", "risk 2", "risk 3"],
  "management_tone": "bullish" | "cautiously_optimistic" | "neutral" | "cautious" | "bearish",
  "confidence_level": "high" | "medium" | "low",
  "ai_summary": "3-4 sentence executive summary of the concall: results vs expectations, key guidance, and outlook"
}}

Rules:
- If management gives a range (e.g. "15-20% growth"), set range_low_pct=15, range_high_pct=20, type="quantified"
- If management says "double-digit growth" set magnitude="moderate", direction="growth", type="directional"
- If management says "strong double-digit growth" set magnitude="strong", direction="growth", type="directional"
- Quotes must be verbatim from the transcript, maximum 120 characters
- If a guidance dimension was not discussed, set given=false and all other fields null
- Return ONLY the JSON, nothing else

TRANSCRIPT:
{transcript}"""


def _extract_guidance_claude(ticker: str, company_name: str,
                              quarter: str, transcript: str) -> dict:
    """Call Claude Haiku to extract structured guidance from transcript."""
    client = _get_anthropic_client()
    prompt = EXTRACTION_PROMPT.format(
        company_name=company_name,
        ticker=ticker,
        quarter=quarter,
        transcript=transcript[:100_000],    # trim to stay in context
    )
    try:
        msg = client.messages.create(
            model=_bulk_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"{ticker} {quarter}: JSON parse error: {e}")
        return {}
    except Exception as e:
        logger.error(f"{ticker} {quarter}: Claude API error: {e}")
        return {}


# ── guidance → re_guidance rows ───────────────────────────────────────────────

def _build_guidance_rows(ticker: str, quarter: str, extracted: dict) -> list[dict]:
    """Convert extracted guidance dict into re_guidance rows."""
    rows = []
    guidance = extracted.get("guidance", {})

    metric_map = {
        "revenue":         ("revenue",         "for_revenue"),
        "margins":         ("margins",         "for_margins"),
        "volume_or_units": ("volume_or_units", "for_volume"),
        "capex":           ("capex",           "for_capex"),
    }

    for metric_key, (metric_label, _) in metric_map.items():
        g = guidance.get(metric_key, {})
        if not g or not g.get("given", False):
            continue

        # Determine for_period (e.g., if concall is Q4FY26, guidance usually for FY27 or Q1FY27)
        timeframe = g.get("timeframe", "full_year")
        if timeframe == "next_quarter":
            for_period = _next_quarter(quarter)
        elif timeframe == "full_year":
            for_period = _next_fy(quarter)
        else:
            for_period = _next_fy(quarter)

        row = {
            "ticker":                  ticker,
            "guidance_given_quarter":  quarter,
            "for_period":              for_period,
            "metric":                  metric_label,
            "guidance_type":           g.get("type", "directional"),
            "guided_direction":        g.get("direction"),
            "guided_magnitude":        g.get("magnitude"),
            "guided_value_pct":        g.get("value_pct"),
            "guided_range_low_pct":    g.get("range_low_pct"),
            "guided_range_high_pct":   g.get("range_high_pct"),
            "guided_absolute_cr":      g.get("absolute_cr") or g.get("amount_cr"),
            "guided_quote":            g.get("quote"),
            # actuals will be filled later by pipeline
            "actual_value":            None,
            "actual_direction":        None,
            "actual_pct":              None,
            "hit_miss_score":          None,
            "hit_miss_label":          None,
        }
        rows.append(row)
    return rows


def _next_quarter(q: str) -> str:
    """Q2FY26 → Q3FY26, Q4FY26 → Q1FY27"""
    import re
    m = re.match(r"Q([1-4])FY(\d{2})", q)
    if not m:
        return q
    n, fy = int(m.group(1)), int(m.group(2))
    if n == 4:
        return f"Q1FY{str(fy + 1).zfill(2)}"
    return f"Q{n + 1}FY{fy:02d}"


def _next_fy(q: str) -> str:
    """Q4FY26 → FY27, Q2FY26 → FY27"""
    import re
    m = re.match(r"Q[1-4]FY(\d{2})", q)
    if not m:
        return q
    fy = int(m.group(1))
    return f"FY{str(fy + 1).zfill(2)}"


# ── main: process one company ─────────────────────────────────────────────────

def process_ticker(ticker: str, company_name: str, bse_code: str,
                   target_quarters: list[str] | None = None,
                   delay: float = 1.0) -> tuple[list[str], list[str]]:
    """
    Download and parse concalls for a company.
    Returns (processed_quarters, failed_quarters).
    target_quarters: if set, only process these quarters; else last 4 with concalls.
    """
    filings = _get_bse_concall_filings(bse_code)
    if not filings:
        filings = _get_nse_concall_filings(ticker)
    if not filings:
        logger.warning(f"{ticker}: no concall filings found")
        return [], [ticker]

    # filter to target quarters
    if target_quarters:
        filings = [f for f in filings if f["quarter"] in target_quarters]
    else:
        filings = filings[:4]   # last 4 concalls

    processed, failed = [], []
    for filing in filings:
        q       = filing["quarter"]
        pdf_url = filing["pdf_url"]
        try:
            transcript = _download_and_extract_text(pdf_url)
            if len(transcript) < 200:
                logger.warning(f"{ticker} {q}: transcript too short ({len(transcript)} chars)")
                failed.append(q)
                continue

            extracted = _extract_guidance_claude(ticker, company_name, q, transcript)
            if not extracted:
                failed.append(q)
                continue

            # Store concall record
            concall_row = {
                "ticker":          ticker,
                "quarter":         q,
                "concall_date":    str(filing["date"]) if filing.get("date") else None,
                "pdf_url":         pdf_url,
                "raw_text":        transcript[:50_000],    # store first 50K chars only
                "ai_summary":      extracted.get("ai_summary"),
                "guidance_json":   json.dumps(extracted.get("guidance", {})),
                "sector_outlook":  extracted.get("sector_outlook"),
                "key_positives":   json.dumps(extracted.get("key_positives", [])),
                "key_risks":       json.dumps(extracted.get("key_risks", [])),
                "mgmt_tone":       extracted.get("management_tone"),
                "confidence_level":extracted.get("confidence_level"),
                "parsed_at":       datetime.utcnow().isoformat(),
                "parse_model":     _bulk_model(),
            }
            db.upsert_concall(concall_row)

            # Store guidance rows
            guidance_rows = _build_guidance_rows(ticker, q, extracted)
            if guidance_rows:
                db.upsert_guidance(guidance_rows)

            processed.append(q)
            logger.info(f"{ticker} {q}: parsed OK, {len(guidance_rows)} guidance rows")
            time.sleep(delay)

        except Exception as e:
            logger.error(f"{ticker} {q}: error: {e}")
            failed.append(q)

    return processed, failed


# ── batch runner ──────────────────────────────────────────────────────────────

def run_for_companies(companies: list[dict], target_quarters: list[str] | None = None,
                      progress_cb=None) -> tuple[list[str], list[str], int]:
    """
    Parse concalls for a list of company dicts (must have ticker, name, bse_code).
    Returns (processed_tickers, failed_tickers, total_quarters_parsed).
    """
    processed_all, failed_all, total = [], [], 0
    for i, co in enumerate(companies):
        ticker    = co["ticker"]
        name      = co.get("name", ticker)
        bse_code  = co.get("bse_code", "")
        if progress_cb:
            progress_cb(i, len(companies), ticker)
        if not bse_code:
            logger.warning(f"{ticker}: missing BSE code, skipping")
            failed_all.append(ticker)
            continue
        p, f = process_ticker(ticker, name, bse_code, target_quarters)
        if p:
            processed_all.append(ticker)
            total += len(p)
        else:
            failed_all.append(ticker)
        time.sleep(2.0)   # extra delay between companies

    return processed_all, failed_all, total


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # Example: python -m modules.concall_parser TCS 532540
    if len(sys.argv) >= 3:
        tk, bsc = sys.argv[1], sys.argv[2]
        name    = sys.argv[3] if len(sys.argv) > 3 else tk
        p, f = process_ticker(tk, name, bsc)
        print(f"Processed quarters: {p} | Failed: {f}")
    else:
        print("Usage: python -m modules.concall_parser TICKER BSE_CODE [Company Name]")
