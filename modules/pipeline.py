"""
pipeline.py — Orchestrates the ResultsEdge data pipeline.
All steps wrapped in try/finally so pipeline logs never get stuck at "running".
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

DATA_DIR     = Path(__file__).parent.parent / "data"
NIFTY50_FILE = DATA_DIR / "nifty50.json"


def seed_companies(filepath: Path = NIFTY50_FILE) -> int:
    with open(filepath) as f:
        companies = json.load(f)
    inserted = db.upsert_companies(companies)
    logger.info(f"Seeded {inserted} companies.")
    return inserted


def run_financials(tickers=None, progress_cb=None) -> dict:
    log_id = db.log_pipeline_start("financials")
    p, f, n = [], [], 0
    try:
        if tickers is None:
            tickers = [c["ticker"] for c in db.get_all_companies()]
        delay = float(db.get_config("screener_delay_seconds", "2.5"))
        logger.info(f"[FINANCIALS] {len(tickers)} tickers")
        p, f, n = run_fin(tickers, delay=delay, progress_cb=progress_cb)
    except Exception as e:
        logger.error(f"[FINANCIALS] Fatal: {e}")
        f.append(f"fatal:{e}")
    finally:
        db.log_pipeline_end(log_id,
                            "completed" if not f else "partial",
                            p, f, n,
                            [str(x) for x in f] if f else [])
    return {"processed": p, "failed": f, "records": n}


def run_concalls(tickers=None, target_quarters=None,
                 max_quarters=4, progress_cb=None) -> dict:
    log_id = db.log_pipeline_start("concalls")
    p, f, n = [], [], 0
    try:
        companies = db.get_all_companies()
        if tickers:
            companies = [c for c in companies if c["ticker"] in tickers]
        logger.info(f"[CONCALLS] {len(companies)} companies")
        p, f, n = run_cc(companies,
                         target_quarters=target_quarters,
                         max_quarters=max_quarters,
                         progress_cb=progress_cb)
    except Exception as e:
        logger.error(f"[CONCALLS] Fatal: {e}")
        f.append(f"fatal:{e}")
    finally:
        db.log_pipeline_end(log_id,
                            "completed" if not f else "partial",
                            p, f, n,
                            [str(x) for x in f] if f else [])
    return {"processed": p, "failed": f, "quarters_parsed": n}


def run_technicals(tickers=None, progress_cb=None) -> dict:
    log_id = db.log_pipeline_start("technicals")
    p, f, n = [], [], 0
    try:
        if tickers is None:
            tickers = [c["ticker"] for c in db.get_all_companies()]
        logger.info(f"[TECHNICALS] {len(tickers)} tickers")
        p, f, n = run_tech(tickers, progress_cb=progress_cb)
    except Exception as e:
        logger.error(f"[TECHNICALS] Fatal: {e}")
        f.append(f"fatal:{e}")
    finally:
        db.log_pipeline_end(log_id,
                            "completed" if not f else "partial",
                            p, f, n,
                            [str(x) for x in f] if f else [])
    return {"processed": p, "failed": f, "records": n}


def run_scoring(tickers=None, quarter=None, progress_cb=None) -> dict:
    if quarter is None:
        quarter = db.get_config("active_quarter", "Q4FY26")
    log_id = db.log_pipeline_start("scoring")
    p, f = [], []
    try:
        if tickers is None:
            tickers = [c["ticker"] for c in db.get_all_companies()]
        logger.info(f"[SCORING] {len(tickers)} tickers for {quarter}")
        p, f = run_scoring_for_all(tickers, quarter, progress_cb=progress_cb)
    except Exception as e:
        logger.error(f"[SCORING] Fatal: {e}")
        f.append(f"fatal:{e}")
    finally:
        db.log_pipeline_end(log_id,
                            "completed" if not f else "partial",
                            p, f, len(p),
                            [str(x) for x in f] if f else [])
    return {"processed": p, "failed": f, "quarter": quarter}


def run_full(tickers=None, progress_cb=None) -> dict:
    log_id  = db.log_pipeline_start("full")
    results = {}
    all_p, all_f = [], []
    try:
        for name, fn in [
            ("financials",  lambda: run_financials(tickers, progress_cb)),
            ("concalls",    lambda: run_concalls(tickers, progress_cb=progress_cb)),
            ("technicals",  lambda: run_technicals(tickers, progress_cb)),
            ("scoring",     lambda: run_scoring(tickers, progress_cb=progress_cb)),
        ]:
            logger.info(f"[FULL] step: {name}")
            try:
                r = fn()
                results[name] = r
                all_p.extend(r.get("processed", []))
                all_f.extend(r.get("failed",    []))
            except Exception as e:
                logger.error(f"[FULL] {name} failed: {e}")
                results[name] = {"error": str(e)}
    finally:
        db.log_pipeline_end(log_id,
                            "completed" if not all_f else "partial",
                            list(set(all_p)), list(set(all_f)),
                            sum(r.get("records", 0) for r in results.values()
                                if isinstance(r, dict) and "error" not in r),
                            [])
    return results
