"""
financial_parser.py — Scrape quarterly financials from Screener.in
Fetches last 8 quarters: revenue, EBITDA, PAT, EPS, OPM, interest, depreciation.
Computes YoY and QoQ growth. Stores in re_financials via db.py.
"""
import re
import time
import logging
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from modules import db

logger = logging.getLogger(__name__)

SCREENER_BASE    = "https://www.screener.in/company"
SESSION_HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.screener.in/",
}
REQUEST_TIMEOUT  = 20
DEFAULT_DELAY    = 2.5          # seconds between requests — be polite


# ── quarter helpers ───────────────────────────────────────────────────────────

def _col_to_quarter(col_header: str) -> str | None:
    """
    Convert Screener.in column header to standard quarter string.
    Examples: "Jun 2024" → "Q1FY25", "Mar 2026" → "Q4FY26"
    """
    month_to_q = {"Jun": "Q1", "Sep": "Q2", "Dec": "Q3", "Mar": "Q4"}
    parts = col_header.strip().split()
    if len(parts) != 2:
        return None
    month, year_str = parts
    q = month_to_q.get(month)
    if not q or not year_str.isdigit():
        return None
    yr = int(year_str)
    fy = yr if month == "Mar" else yr + 1
    return f"{q}FY{str(fy)[-2:]}"


def _quarter_end_date(quarter: str) -> date | None:
    """Q1FY26 → 2025-06-30, Q4FY26 → 2026-03-31"""
    m = re.match(r"Q([1-4])FY(\d{2})", quarter)
    if not m:
        return None
    q_num = int(m.group(1))
    fy    = 2000 + int(m.group(2))
    q_months = {1: (6, 30), 2: (9, 30), 3: (12, 31), 4: (3, 31)}
    mon, day = q_months[q_num]
    year = fy - 1 if q_num == 4 else fy - 1  # Q4FY26 → Mar 2026 = year 2026-1=2025? No.
    # FY26 = Apr2025-Mar2026
    # Q1FY26 = Jun 2025 → year 2025
    # Q4FY26 = Mar 2026 → year 2026
    fy_end_year = fy
    if q_num in (1, 2, 3):
        cal_year = fy_end_year - 1
    else:
        cal_year = fy_end_year
    try:
        return date(cal_year, mon, day)
    except ValueError:
        return None


def _parse_number(cell) -> float | None:
    """Extract numeric value from a BS4 tag or string."""
    if cell is None:
        return None
    text = cell.get_text(strip=True) if hasattr(cell, "get_text") else str(cell)
    text = text.replace(",", "").replace("%", "").strip()
    if not text or text in ("-", "—", ""):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ── main scraper ──────────────────────────────────────────────────────────────

def fetch_financials(ticker: str, session: requests.Session) -> list[dict]:
    """
    Fetch quarterly P&L data for *ticker* from Screener.in.
    Returns a list of dicts (one per quarter), newest first.
    """
    for suffix in ["/consolidated/", "/"]:
        url = f"{SCREENER_BASE}/{ticker}{suffix}"
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            rows = _parse_quarterly_table(resp.text, ticker)
            if rows:
                logger.info(f"{ticker}: scraped {len(rows)} quarters from {url}")
                return rows
        except requests.RequestException as e:
            logger.warning(f"{ticker}: request error at {url}: {e}")
    logger.error(f"{ticker}: could not fetch financials from Screener.in")
    return []


def _parse_quarterly_table(html: str, ticker: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    section = soup.find("section", id="quarters")
    if not section:
        return []

    table = section.find("table")
    if not table:
        return []

    # ── header row → quarter labels ─────────────────────────────────────────
    headers = []
    thead = table.find("thead")
    if thead:
        ths = thead.find_all("th")
        # first <th> is the row-label column
        headers = [th.get_text(strip=True) for th in ths[1:]]

    quarters = [_col_to_quarter(h) for h in headers]

    # ── data rows ────────────────────────────────────────────────────────────
    data: dict[str, dict] = {q: {} for q in quarters if q}

    row_key_map = {
        "Sales":              "revenue_cr",
        "Net Sales":          "revenue_cr",
        "Revenue":            "revenue_cr",
        "Operating Profit":   "ebitda_cr",
        "OPM %":              "opm_pct",
        "Net Profit":         "pat_cr",
        "EPS in Rs":          "eps",
        "Interest":           "interest_cr",
        "Depreciation":       "depreciation_cr",
    }

    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = tds[0].get_text(strip=True).rstrip(" +")

        # Match the label
        field = None
        for key, col in row_key_map.items():
            if label.startswith(key):
                field = col
                break
        if not field:
            continue

        for i, q in enumerate(quarters):
            if q and i + 1 < len(tds):
                val = _parse_number(tds[i + 1])
                if val is not None:
                    data[q][field] = val

    # ── compute EBITDA margin if OPM not on page ──────────────────────────
    for q, vals in data.items():
        if "opm_pct" not in vals and vals.get("revenue_cr") and vals.get("ebitda_cr"):
            rev = vals["revenue_cr"]
            ebit = vals["ebitda_cr"]
            vals["opm_pct"] = round(ebit / rev * 100, 2) if rev else None
        # True EBITDA = Operating Profit + Depreciation
        if vals.get("ebitda_cr") and vals.get("depreciation_cr"):
            vals["ebitda_cr"] = round(vals["ebitda_cr"] + vals["depreciation_cr"], 2)

    # ── sort newest → oldest ──────────────────────────────────────────────
    sorted_qs = sorted(
        [q for q in quarters if q and data.get(q)],
        key=lambda x: _quarter_end_date(x) or date.min,
        reverse=True
    )

    if not sorted_qs:
        return []

    # ── compute YoY / QoQ growth ──────────────────────────────────────────
    result_rows = []
    for idx, q in enumerate(sorted_qs):
        row = {
            "ticker":          ticker,
            "quarter":         q,
            "quarter_end_date": str(_quarter_end_date(q)) if _quarter_end_date(q) else None,
            "data_source":     "screener",
        }
        row.update(data[q])

        # YoY: compare with same quarter 4 positions back
        if idx + 4 < len(sorted_qs):
            yoy_q = sorted_qs[idx + 4]
            rev_now  = data[q].get("revenue_cr")
            rev_then = data[yoy_q].get("revenue_cr")
            if rev_now and rev_then and rev_then != 0:
                row["revenue_yoy"] = round((rev_now - rev_then) / rev_then * 100, 2)
            pat_now  = data[q].get("pat_cr")
            pat_then = data[yoy_q].get("pat_cr")
            if pat_now and pat_then and pat_then != 0:
                row["pat_yoy"] = round((pat_now - pat_then) / pat_then * 100, 2)

        # QoQ: compare with previous quarter
        if idx + 1 < len(sorted_qs):
            prev_q    = sorted_qs[idx + 1]
            rev_now   = data[q].get("revenue_cr")
            rev_prev  = data[prev_q].get("revenue_cr")
            if rev_now and rev_prev and rev_prev != 0:
                row["revenue_qoq"] = round((rev_now - rev_prev) / rev_prev * 100, 2)

        result_rows.append(row)

    return result_rows[:8]   # cap at 8 quarters


# ── batch runner ──────────────────────────────────────────────────────────────

def run_for_tickers(tickers: list[str], delay: float = DEFAULT_DELAY,
                    progress_cb=None) -> tuple[list[str], list[str], int]:
    """
    Fetch and store financials for a list of tickers.
    Returns (processed, failed, total_records_inserted).
    """
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)

    processed, failed, total = [], [], 0

    for i, ticker in enumerate(tickers):
        if progress_cb:
            progress_cb(i, len(tickers), ticker)
        try:
            rows = fetch_financials(ticker, session)
            if rows:
                db.upsert_financials(rows)
                total += len(rows)
                processed.append(ticker)
            else:
                failed.append(ticker)
        except Exception as e:
            logger.error(f"{ticker}: unhandled error: {e}")
            failed.append(ticker)
        time.sleep(delay)

    return processed, failed, total


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["TCS", "INFY", "HDFCBANK"]
    print(f"Fetching financials for: {tickers}")
    p, f, n = run_for_tickers(tickers)
    print(f"Done. Processed: {p}, Failed: {f}, Records: {n}")
