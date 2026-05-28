"""
db.py — Supabase client + CRUD helpers for ResultsEdge
Project: aiebaqvclyzxajigvkfd (NSE Scanner Pro, ap-south-1)
Tables: re_companies, re_financials, re_concall, re_guidance,
        re_scores, re_technical, re_pipeline_logs, re_config
"""
import json
import os
from datetime import datetime, timezone
from typing import Any

import streamlit as st
from supabase import create_client, Client


# ── connection ────────────────────────────────────────────────────────────────

def get_client() -> Client:
    """Return a Supabase client, reading credentials from st.secrets or env."""
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
    except Exception:
        url = os.getenv("SUPABASE_URL", "https://aiebaqvclyzxajigvkfd.supabase.co")
        key = os.getenv("SUPABASE_KEY", "")
    return create_client(url, key)


# ── config ────────────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    sb = get_client()
    res = sb.table("re_config").select("value").eq("key", key).limit(1).execute()
    rows = res.data or []
    return rows[0]["value"] if rows else default


def set_config(key: str, value: str) -> None:
    sb = get_client()
    sb.table("re_config").upsert(
        {"key": key, "value": value, "updated_at": _now()},
        on_conflict="key"
    ).execute()


# ── companies ─────────────────────────────────────────────────────────────────

def upsert_companies(companies: list[dict]) -> int:
    sb = get_client()
    rows = [{
        "ticker":           c["ticker"],
        "name":             c["name"],
        "sector":           c.get("sector"),
        "industry":         c.get("industry"),
        "bse_code":         c.get("bse_code"),
        "nse_code":         c.get("nse_ticker", c["ticker"]),
        "index_membership": json.dumps(c.get("index_membership", ["nifty50"])),
        "is_active":        True,
        "updated_at":       _now(),
    } for c in companies]
    sb.table("re_companies").upsert(rows, on_conflict="ticker").execute()
    return len(rows)


def get_all_companies(index: str = "nifty50") -> list[dict]:
    sb = get_client()
    res = sb.table("re_companies")\
            .select("*")\
            .eq("is_active", True)\
            .execute()
    rows = res.data or []
    if index:
        rows = [r for r in rows if index in (r.get("index_membership") or [])]
    return rows


# ── financials ────────────────────────────────────────────────────────────────

def upsert_financials(rows: list[dict]) -> int:
    if not rows:
        return 0
    sb = get_client()
    sb.table("re_financials").upsert(rows, on_conflict="ticker,quarter").execute()
    return len(rows)


def get_financials(ticker: str, limit: int = 8) -> list[dict]:
    sb = get_client()
    res = sb.table("re_financials")\
            .select("*")\
            .eq("ticker", ticker)\
            .order("quarter_end_date", desc=True)\
            .limit(limit)\
            .execute()
    return res.data or []


def get_latest_financials_all(quarter: str) -> list[dict]:
    """One financial row per company for a specific quarter."""
    sb = get_client()
    res = sb.table("re_financials")\
            .select("*")\
            .eq("quarter", quarter)\
            .execute()
    return res.data or []


# ── concall ───────────────────────────────────────────────────────────────────

def upsert_concall(row: dict) -> None:
    sb = get_client()
    sb.table("re_concall").upsert(row, on_conflict="ticker,quarter").execute()


def get_concall(ticker: str, quarter: str) -> dict | None:
    sb = get_client()
    res = sb.table("re_concall")\
            .select("*")\
            .eq("ticker", ticker)\
            .eq("quarter", quarter)\
            .limit(1)\
            .execute()
    rows = res.data or []
    return rows[0] if rows else None


def get_concalls_for_ticker(ticker: str, limit: int = 4) -> list[dict]:
    sb = get_client()
    res = sb.table("re_concall")\
            .select("*")\
            .eq("ticker", ticker)\
            .order("quarter", desc=True)\
            .limit(limit)\
            .execute()
    return res.data or []


# ── guidance ──────────────────────────────────────────────────────────────────

def upsert_guidance(rows: list[dict]) -> int:
    if not rows:
        return 0
    sb = get_client()
    sb.table("re_guidance").upsert(
        rows,
        on_conflict="ticker,guidance_given_quarter,for_period,metric"
    ).execute()
    return len(rows)


def get_guidance_history(ticker: str, limit: int = 20) -> list[dict]:
    sb = get_client()
    res = sb.table("re_guidance")\
            .select("*")\
            .eq("ticker", ticker)\
            .order("guidance_given_quarter", desc=True)\
            .limit(limit)\
            .execute()
    return res.data or []


def update_guidance_actuals(ticker: str, quarter: str, metric: str,
                             actual_pct: float, actual_direction: str,
                             hit_miss_score: int, hit_miss_label: str) -> None:
    sb = get_client()
    sb.table("re_guidance")\
      .update({
          "actual_pct":       actual_pct,
          "actual_direction": actual_direction,
          "hit_miss_score":   hit_miss_score,
          "hit_miss_label":   hit_miss_label,
      })\
      .eq("ticker", ticker)\
      .eq("for_period", quarter)\
      .eq("metric", metric)\
      .execute()


# ── scores ────────────────────────────────────────────────────────────────────

def upsert_score(row: dict) -> None:
    sb = get_client()
    sb.table("re_scores").upsert(row, on_conflict="ticker,quarter").execute()


def get_scores_leaderboard(quarter: str) -> list[dict]:
    """All company scores for a given quarter, merged with company metadata."""
    sb = get_client()

    # ── scores ───────────────────────────────────────────────────────────
    s_res = sb.table("re_scores")\
              .select("*")\
              .eq("quarter", quarter)\
              .order("composite_score", desc=True)\
              .execute()
    scores = s_res.data or []
    if not scores:
        return []

    # ── companies (separate query — no FK needed) ─────────────────────
    c_res = sb.table("re_companies")\
              .select("ticker,name,sector,industry,index_membership")\
              .eq("is_active", True)\
              .execute()
    company_map = {c["ticker"]: c for c in (c_res.data or [])}

    rows = []
    for s in scores:
        co   = company_map.get(s["ticker"], {})
        idx  = co.get("index_membership") or []
        # Supabase JSONB may return a list or a JSON string
        if isinstance(idx, str):
            try:
                idx = json.loads(idx)
            except Exception:
                idx = []
        s["name"]             = co.get("name", s["ticker"])
        s["sector"]           = co.get("sector", "—")
        s["industry"]         = co.get("industry", "—")
        s["index_membership"] = idx
        rows.append(s)
    return rows


def get_score(ticker: str, quarter: str) -> dict | None:
    sb = get_client()
    res = sb.table("re_scores")\
            .select("*")\
            .eq("ticker", ticker)\
            .eq("quarter", quarter)\
            .limit(1)\
            .execute()
    rows = res.data or []
    return rows[0] if rows else None


# ── technical ─────────────────────────────────────────────────────────────────

def upsert_technical(rows: list[dict]) -> int:
    if not rows:
        return 0
    sb = get_client()
    sb.table("re_technical").upsert(rows, on_conflict="ticker,as_of_date").execute()
    return len(rows)


def get_latest_technical(ticker: str) -> dict | None:
    sb = get_client()
    res = sb.table("re_technical")\
            .select("*")\
            .eq("ticker", ticker)\
            .order("as_of_date", desc=True)\
            .limit(1)\
            .execute()
    rows = res.data or []
    return rows[0] if rows else None


# ── pipeline logs ─────────────────────────────────────────────────────────────

def log_pipeline_start(run_type: str) -> int:
    sb = get_client()
    res = sb.table("re_pipeline_logs").insert({
        "run_type":   run_type,
        "status":     "running",
        "started_at": _now(),
    }).execute()
    return res.data[0]["id"] if res.data else -1


def log_pipeline_end(log_id: int, status: str, processed: list,
                     failed: list, inserted: int, errors: list) -> None:
    if log_id < 0:
        return
    sb = get_client()
    start_res = sb.table("re_pipeline_logs")\
                  .select("started_at")\
                  .eq("id", log_id)\
                  .limit(1)\
                  .execute()
    duration = None
    rows = start_res.data or []
    if rows:
        st_dt = datetime.fromisoformat(rows[0]["started_at"].replace("Z", "+00:00"))
        duration = int((datetime.now(timezone.utc) - st_dt).total_seconds())

    sb.table("re_pipeline_logs").update({
        "status":             status,
        "tickers_processed":  json.dumps(processed),
        "tickers_failed":     json.dumps(failed),
        "records_inserted":   inserted,
        "error_messages":     json.dumps(errors),
        "completed_at":       _now(),
        "duration_seconds":   duration,
    }).eq("id", log_id).execute()


def get_recent_logs(limit: int = 20) -> list[dict]:
    sb = get_client()
    res = sb.table("re_pipeline_logs")\
            .select("*")\
            .order("started_at", desc=True)\
            .limit(limit)\
            .execute()
    return res.data or []


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
