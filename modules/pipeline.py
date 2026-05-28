"""
pipeline.py — Orchestrates the full ResultsEdge data pipeline.

Steps:
  1. seed_companies       → insert company master into re_companies
  2. run_financials       → Screener.in scraper for all tickers
  3. run_concalls         → BSE PDF download + Claude AI parsing
  4. run_technicals       → Breeze API price/momentum data
  5. run_scoring          → compute all 10 scores + composite
  6. run_full             → all of the above in sequence
"""
import json
import logging
import time
from pathlib import Path

from modules import db
from modules.financial_parser import run_for_tickers as run_fin
from modules.concall_parser   import run_for_companies as run_cc
from modules.technical        import run_for_tickers   as run_tech
from modules.scoring          import run_scoring_for_all

logger = logging.getLogger(__name__)

DATA_DIR       = Path(__file__).parent.parent / "data"
NIFTY50_FILE   = DATA_DIR / "nifty50.json"


# ── company master seeding ────────────────────────────────────────────────────

def seed_companies(filepath: Path = NIFTY50_FILE) -> int:
    """Insert/update Nifty 50 companies into re_companies."""
    with open(filepath, "r") as f:
        companies = json.load(f)
    inserted = db.upsert_companies(companies)
    logger.info(f"Seeded {inserted} companies into re_companies.")
    return inserted


# ── individual step runners ───────────────────────────────────────────────────

def run_financials(tickers: list[str] | None = None,
                   progress_cb=None) -> dict:
    """Fetch financials from Screener.in."""
    log_id = db.log_pipeline_start("financials")
    if tickers is None:
        companies = db.get_all_companies()
        tickers   = [c["ticker"] for c in companies]

    logger.info(f"[FINANCIALS] Starting for {len(tickers)} tickers")
    delay  = float(db.get_config("screener_delay_seconds", "2.5"))
    p, f, n = run_fin(tickers, delay=delay, progress_cb=progress_cb)

    db.log_pipeline_end(log_id, "completed" if not f else "partial",
                        p, f, n, [f"Failed: {','.join(f)}"] if f else [])
    summary = {"processed": p, "failed": f, "records": n}
    logger.info(f"[FINANCIALS] Done. {summary}")
    return summary


def run_concalls(tickers: list[str] | None = None,
                 target_quarters: list[str] | None = None,
                 progress_cb=None) -> dict:
    """Download and parse concall PDFs via Claude AI."""
    log_id = db.log_pipeline_start("concalls")
    companies = db.get_all_companies()
    if tickers:
        companies = [c for c in companies if c["ticker"] in tickers]

    logger.info(f"[CONCALLS] Starting for {len(companies)} companies")
    p, f, n = run_cc(companies,
                     target_quarters=target_quarters,
                     progress_cb=progress_cb)
    db.log_pipeline_end(log_id, "completed" if not f else "partial",
                        p, f, n, [f"Failed: {','.join(f)}"] if f else [])
    summary = {"processed": p, "failed": f, "quarters_parsed": n}
    logger.info(f"[CONCALLS] Done. {summary}")
    return summary


def run_technicals(tickers: list[str] | None = None,
                   progress_cb=None) -> dict:
    """Fetch Breeze API price data and compute RS/momentum."""
    log_id = db.log_pipeline_start("technicals")
    if tickers is None:
        companies = db.get_all_companies()
        tickers   = [c["ticker"] for c in companies]

    logger.info(f"[TECHNICALS] Starting for {len(tickers)} tickers")
    p, f, n = run_tech(tickers, progress_cb=progress_cb)
    db.log_pipeline_end(log_id, "completed" if not f else "partial",
                        p, f, n, [f"Failed: {','.join(f)}"] if f else [])
    summary = {"processed": p, "failed": f, "records": n}
    logger.info(f"[TECHNICALS] Done. {summary}")
    return summary


def run_scoring(tickers: list[str] | None = None,
                quarter: str | None = None,
                progress_cb=None) -> dict:
    """Compute composite scores for all tickers."""
    if quarter is None:
        quarter = db.get_config("active_quarter", "Q4FY26")

    log_id = db.log_pipeline_start("scoring")
    if tickers is None:
        companies = db.get_all_companies()
        tickers   = [c["ticker"] for c in companies]

    logger.info(f"[SCORING] Starting for {len(tickers)} tickers, quarter={quarter}")
    p, f = run_scoring_for_all(tickers, quarter, progress_cb=progress_cb)
    db.log_pipeline_end(log_id, "completed" if not f else "partial",
                        p, f, len(p), [f"Failed: {','.join(f)}"] if f else [])
    summary = {"processed": p, "failed": f, "quarter": quarter}
    logger.info(f"[SCORING] Done. {summary}")
    return summary


def run_full(tickers: list[str] | None = None,
             progress_cb=None) -> dict:
    """
    Full pipeline: financials → concalls → technicals → scoring.
    """
    log_id   = db.log_pipeline_start("full")
    results  = {}
    all_p, all_f = [], []

    steps = [
        ("financials",  lambda: run_financials(tickers, progress_cb)),
        ("concalls",    lambda: run_concalls(tickers, progress_cb=progress_cb)),
        ("technicals",  lambda: run_technicals(tickers, progress_cb)),
        ("scoring",     lambda: run_scoring(tickers, progress_cb=progress_cb)),
    ]

    for name, fn in steps:
        logger.info(f"[FULL] → Step: {name}")
        try:
            r = fn()
            results[name] = r
            all_p.extend(r.get("processed", []))
            all_f.extend(r.get("failed", []))
        except Exception as e:
            logger.error(f"[FULL] Step {name} failed: {e}")
            results[name] = {"error": str(e)}
            all_f.append(f"step:{name}")

    db.log_pipeline_end(log_id,
                        "completed" if not all_f else "partial",
                        list(set(all_p)), list(set(all_f)),
                        sum(r.get("records", 0) for r in results.values() if isinstance(r, dict)),
                        [])
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"
    tickers = sys.argv[2:] if len(sys.argv) > 2 else None

    if cmd == "seed":
        n = seed_companies()
        print(f"Seeded {n} companies.")
    elif cmd == "financials":
        r = run_financials(tickers)
        print(r)
    elif cmd == "concalls":
        r = run_concalls(tickers)
        print(r)
    elif cmd == "technicals":
        r = run_technicals(tickers)
        print(r)
    elif cmd == "scoring":
        r = run_scoring(tickers)
        print(r)
    elif cmd == "full":
        r = run_full(tickers)
        print(r)
    else:
        print("Usage: python -m modules.pipeline [seed|financials|concalls|technicals|scoring|full] [TICKER ...]")
