"""
equisense.py — EquiSense AI research overlay for ResultsEdge.

IMPORTANT — scope boundary:
This module is a QUALITATIVE overlay only. It is never called from
scoring.py and never writes to re_scores. EquiSense narrative is
sourced independently of Screener/BSE-concall data and is displayed
in its own clearly-labeled panel so provenance stays unambiguous.
Quarter-over-quarter score comparisons remain Screener/concall-only.

Cached in re_equisense_cache so repeated views of the same company
don't re-hit the API on every page load. Called on-demand from the
UI (company deep-dive), never from the batch pipeline.
"""
import json
import logging
from datetime import datetime, timezone

from modules import db

logger = logging.getLogger(__name__)

CACHE_TABLE = "re_equisense_cache"
CACHE_TTL_HOURS = 24  # narrative context doesn't need to be minute-fresh


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(ticker: str, quarter: str) -> str:
    return f"{ticker}:{quarter}"


def get_cached(ticker: str, quarter: str) -> dict | None:
    """Return cached EquiSense narrative if fresh enough, else None."""
    sb = db.get_client()
    try:
        res = sb.table(CACHE_TABLE) \
                .select("*") \
                .eq("ticker", ticker) \
                .eq("quarter", quarter) \
                .limit(1) \
                .execute()
    except Exception as e:
        logger.warning(f"EquiSense cache read failed (table may not exist yet): {e}")
        return None

    rows = res.data or []
    if not rows:
        return None

    row = rows[0]
    fetched_at = row.get("fetched_at")
    if not fetched_at:
        return None
    try:
        age_hours = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                     ).total_seconds() / 3600
    except Exception:
        return None

    if age_hours > CACHE_TTL_HOURS:
        return None
    return row


def store_cache(ticker: str, quarter: str, isin: str | None,
                 answer_text: str, company_name: str | None,
                 follow_ups: list[str] | None) -> None:
    sb = db.get_client()
    row = {
        "ticker":       ticker,
        "quarter":      quarter,
        "isin":         isin,
        "answer_text":  answer_text,
        "company_name": company_name,
        "follow_ups":   json.dumps(follow_ups or []),
        "fetched_at":   _now(),
        "source":       "equisense",
    }
    try:
        sb.table(CACHE_TABLE).upsert(row, on_conflict="ticker,quarter").execute()
    except Exception as e:
        logger.warning(f"EquiSense cache write failed (table may not exist yet): {e}")


def build_query(company_name: str, ticker: str) -> str:
    """
    Query is scoped to narrative/qualitative content only —
    deliberately NOT asking for numbers we'd otherwise treat as
    authoritative for scoring purposes.
    """
    return (
        f"Give a concise qualitative research summary for {company_name} "
        f"({ticker}): current bull case, key strategic catalysts, and key "
        f"risks or watch items. Focus on narrative and strategic context, "
        f"not detailed financial line items."
    )


# NOTE: The actual ask_equisense tool call happens in app.py, not here —
# this module owns caching/formatting logic only, since the MCP tool
# call itself must be issued by the assistant/orchestration layer, not
# from within the Streamlit process. See app.py `_get_equisense_panel()`
# for the call site and integration notes if porting this to a backend
# job runner instead.
