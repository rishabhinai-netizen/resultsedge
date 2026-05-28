"""
technical.py — Fetch daily OHLCV via Breeze API, compute RS (63-day vs Nifty),
price vs 200/50 DMA, 52-week rank, volume ratio, and a composite momentum_score.
Stores results in re_technical via db.py.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta

import streamlit as st

from modules import db

logger = logging.getLogger(__name__)

NIFTY_BREEZE_CODE = "CNXNIFTY"
LOOKBACK_DAYS     = 300          # enough for 200DMA + 63-day RS


# ── Breeze client ─────────────────────────────────────────────────────────────

def _get_breeze():
    try:
        from breeze_connect import BreezeConnect
    except ImportError:
        raise RuntimeError("breeze-connect not installed. Run: pip install breeze-connect")

    try:
        api_key   = st.secrets["BREEZE_API_KEY"]
        api_secret= st.secrets["BREEZE_API_SECRET"]
        session   = st.secrets["BREEZE_SESSION_TOKEN"]
    except Exception:
        api_key   = os.getenv("BREEZE_API_KEY", "")
        api_secret= os.getenv("BREEZE_API_SECRET", "")
        session   = os.getenv("BREEZE_SESSION_TOKEN", "")

    if not api_key or not session:
        raise RuntimeError("Breeze API credentials not configured in secrets.")

    breeze = BreezeConnect(api_key=api_key)
    breeze.generate_session(api_secret=api_secret, session_token=session)
    return breeze


# ── data fetch ────────────────────────────────────────────────────────────────

def _fetch_daily_prices(breeze, stock_code: str,
                         from_date: str, to_date: str) -> list[float]:
    """
    Fetch daily close prices for *stock_code* between from_date and to_date.
    Returns list of close prices sorted oldest→newest.
    """
    resp = breeze.get_historical_data_v2(
        interval="1day",
        from_date=f"{from_date}T07:00:00.000Z",
        to_date=f"{to_date}T07:00:00.000Z",
        stock_code=stock_code,
        exchange_code="NSE",
        product_type="cash",
    )
    if resp.get("Status") != 200:
        raise RuntimeError(f"Breeze error for {stock_code}: {resp.get('Error','unknown')}")
    rows = resp.get("Success", [])
    prices = []
    for r in rows:
        try:
            prices.append(float(r["close"]))
        except (KeyError, ValueError):
            pass
    return prices


# ── indicator calculations ────────────────────────────────────────────────────

def _rs_percentile(stock_prices: list[float], nifty_prices: list[float],
                    window: int = 63) -> float | None:
    """
    63-day Relative Strength = stock return / nifty return over last 63 trading days.
    Returns the absolute RS ratio (not a percentile — call from batch to rank).
    """
    if len(stock_prices) < window + 1 or len(nifty_prices) < window + 1:
        return None
    s_ret = (stock_prices[-1] - stock_prices[-window - 1]) / stock_prices[-window - 1]
    n_ret = (nifty_prices[-1] - nifty_prices[-window - 1]) / nifty_prices[-window - 1]
    if n_ret == 0:
        return None
    return round(s_ret / n_ret * 100, 2)   # >100 means outperforming Nifty


def _pct_vs_dma(prices: list[float], window: int) -> float | None:
    if len(prices) < window:
        return None
    dma = sum(prices[-window:]) / window
    current = prices[-1]
    return round((current - dma) / dma * 100, 2)


def _rank_52w(prices: list[float]) -> float | None:
    """0-100 percentile of current price within 52-week range."""
    if len(prices) < 2:
        return None
    window = min(252, len(prices))
    recent = prices[-window:]
    lo, hi = min(recent), max(recent)
    if hi == lo:
        return 50.0
    return round((prices[-1] - lo) / (hi - lo) * 100, 2)


def _volume_ratio(volumes: list[float], window: int = 20) -> float | None:
    if len(volumes) < window + 1:
        return None
    avg = sum(volumes[-window - 1:-1]) / window
    if avg == 0:
        return None
    return round(volumes[-1] / avg, 2)


def _momentum_score(rs: float | None, vs200: float | None, rank52: float | None) -> float:
    """Compute composite momentum score 1-10."""
    def rs_s(v):
        if v is None: return 5
        if v > 150: return 10
        if v > 130: return 9
        if v > 115: return 8
        if v > 105: return 7
        if v > 100: return 6
        if v > 90:  return 5
        if v > 75:  return 4
        if v > 60:  return 3
        return 2

    def ma_s(v):
        if v is None: return 5
        if v > 30:  return 10
        if v > 20:  return 9
        if v > 10:  return 7
        if v > 5:   return 6
        if v >= 0:  return 5
        if v > -10: return 3
        return 2

    def rk_s(v):
        if v is None: return 5
        if v > 90: return 10
        if v > 75: return 8
        if v > 60: return 7
        if v > 50: return 6
        if v > 35: return 5
        if v > 25: return 4
        return 2

    return round(0.50 * rs_s(rs) + 0.30 * ma_s(vs200) + 0.20 * rk_s(rank52), 1)


# ── main: process one ticker ──────────────────────────────────────────────────

def compute_technical(ticker: str, breeze, nifty_prices: list[float],
                       as_of: date | None = None) -> dict | None:
    """
    Compute all technical indicators for *ticker*.
    nifty_prices must already be fetched (passed in to avoid re-fetching).
    Returns a re_technical row dict or None on failure.
    """
    if as_of is None:
        as_of = date.today()

    from_date = (datetime.combine(as_of, datetime.min.time()) -
                 timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    to_date   = as_of.strftime("%Y-%m-%d")

    try:
        resp = breeze.get_historical_data_v2(
            interval="1day",
            from_date=f"{from_date}T07:00:00.000Z",
            to_date=f"{to_date}T07:00:00.000Z",
            stock_code=ticker,
            exchange_code="NSE",
            product_type="cash",
        )
        if resp.get("Status") != 200:
            logger.warning(f"{ticker}: Breeze error: {resp.get('Error')}")
            return None

        rows     = resp.get("Success", [])
        prices   = [float(r["close"])  for r in rows if r.get("close")]
        volumes  = [float(r["volume"]) for r in rows if r.get("volume")]
        curr     = prices[-1] if prices else None

    except Exception as e:
        logger.error(f"{ticker}: Breeze fetch error: {e}")
        return None

    if not prices or curr is None:
        return None

    rs   = _rs_percentile(prices, nifty_prices)
    v200 = _pct_vs_dma(prices, 200)
    v50  = _pct_vs_dma(prices, 50)
    rk   = _rank_52w(prices)
    vol  = _volume_ratio(volumes, 20)
    mom  = _momentum_score(rs, v200, rk)

    return {
        "ticker":          ticker,
        "as_of_date":      str(as_of),
        "close_price":     round(curr, 2),
        "rs_63d":          rs,
        "pct_vs_200dma":   v200,
        "pct_vs_50dma":    v50,
        "rank_52w":        rk,
        "volume_ratio_20d":vol,
        "momentum_score":  mom,
    }


# ── batch runner ──────────────────────────────────────────────────────────────

def run_for_tickers(tickers: list[str], progress_cb=None) -> tuple[list[str], list[str], int]:
    """
    Compute and store technical data for a list of tickers.
    Returns (processed, failed, total_records).
    """
    try:
        breeze = _get_breeze()
    except RuntimeError as e:
        logger.error(f"Cannot run technical module: {e}")
        return [], tickers, 0

    as_of     = date.today()
    from_date = (datetime.combine(as_of, datetime.min.time()) -
                 timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    to_date   = as_of.strftime("%Y-%m-%d")

    # Fetch Nifty prices once (shared denominator for RS)
    try:
        nifty_prices = _fetch_daily_prices(breeze, NIFTY_BREEZE_CODE, from_date, to_date)
        logger.info(f"Nifty: {len(nifty_prices)} daily bars fetched")
    except Exception as e:
        logger.error(f"Nifty fetch failed: {e}")
        return [], tickers, 0

    processed, failed, rows_out = [], [], []
    for i, ticker in enumerate(tickers):
        if progress_cb:
            progress_cb(i, len(tickers), ticker)
        try:
            row = compute_technical(ticker, breeze, nifty_prices, as_of)
            if row:
                rows_out.append(row)
                processed.append(ticker)
            else:
                failed.append(ticker)
        except Exception as e:
            logger.error(f"{ticker}: unhandled error: {e}")
            failed.append(ticker)
        time.sleep(0.3)    # gentle rate limit

    if rows_out:
        db.upsert_technical(rows_out)

    return processed, failed, len(rows_out)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["TCS", "INFY", "HDFCBANK"]
    p, f, n = run_for_tickers(tickers)
    print(f"Done. Processed: {p}, Failed: {f}, Records: {n}")
