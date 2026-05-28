# ResultsEdge

**NSE Earnings Intelligence Platform — Nifty 50 → 200 → 500**

Systematic scoring of every listed company on 10 parameters post-quarterly results.
Composite score out of 100. Walk-the-talk engine compares every piece of management guidance vs actual delivery over 8 quarters.

---

## Scoring Framework

| # | Parameter | Weight | Data source |
|---|---|---|---|
| S1 | Revenue growth quality | 10% | Screener.in |
| S2 | Profitability expansion | 10% | Screener.in |
| S3 | Cash flow quality | 5% | Screener.in |
| S4 | Guidance quality | 10% | BSE concall + Claude AI |
| S5 | Walk the talk (historical, 4Q) | **20%** | BSE concall + Claude AI |
| S6 | Current quarter delivery | 10% | BSE concall + Claude AI |
| S7 | Sector position (peer rank) | 10% | Screener.in (all peers) |
| S8 | Sector opportunity | 5% | BSE concall + Claude AI |
| S9 | RS & Momentum | 10% | Breeze API |
| S10 | Valuation context | 10% | Screener.in |

**Rating labels:** 85–100 = Excellent · 70–84 = Strong · 55–69 = Average · 40–54 = Weak · <40 = Poor

---

## Tech Stack

- **Frontend:** Streamlit
- **Database:** Supabase (`aiebaqvclyzxajigvkfd`, ap-south-1) — tables prefixed `re_`
- **Financials:** Screener.in (scraper, polite 2.5s delay)
- **Concall PDFs:** BSE filing API + PyMuPDF + Claude AI (Haiku 4.5)
- **Price/RS data:** Breeze API (ICICI Direct)
- **Scheduler:** GitHub Actions (weekly + on-demand)

---

## Deployment — Streamlit Cloud

### 1. Fork / clone this repo to your GitHub account (already done)

### 2. Add Streamlit secrets

Go to `https://share.streamlit.io` → your app → **Settings → Secrets**.
Paste this block (fill in your values):

```toml
SUPABASE_URL  = "https://aiebaqvclyzxajigvkfd.supabase.co"
SUPABASE_KEY  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFpZWJhcXZjbHl6eGFqaWd2a2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ5NTg1MDQsImV4cCI6MjA5MDUzNDUwNH0.m_WLKdaKwEw82RRepHYhXp3tg-g0pwMiDKM2S7Y7XdY"

ANTHROPIC_API_KEY = "sk-ant-..."       # Your Anthropic key

BREEZE_API_KEY        = "..."          # From ICICI Direct portal
BREEZE_API_SECRET     = "..."          # From ICICI Direct portal
BREEZE_SESSION_TOKEN  = "..."          # Refresh daily from ICICI Direct
```

### 3. Deploy

```
App file: app.py
Python version: 3.11
```

### 4. First run — seed data

After deploy, go to **⚙️ Pipeline** tab:
1. Click **🌱 Seed companies** — inserts all 50 companies
2. Click **📑 Fetch financials** — scrapes Screener.in (takes ~3 min for 50 companies)
3. Click **🎙️ Parse concalls** — downloads BSE PDFs and runs Claude AI (takes ~15 min)
4. Click **📈 Update technicals** — fetches Breeze API price data (needs valid session token)
5. Click **🧮 Run scoring** — computes all 10 scores and composite

Or use **▶ Run full pipeline** to do all at once.

---

## Local development

```bash
git clone https://github.com/rishabhinai-netizen/resultsedge
cd resultsedge
pip install -r requirements.txt

# Create .streamlit/secrets.toml with the block above

streamlit run app.py
```

---

## Pipeline CLI (batch runs / GitHub Actions)

```bash
# Seed company master
python -m modules.pipeline seed

# Fetch financials for specific tickers
python -m modules.pipeline financials TCS INFY HDFCBANK

# Parse concalls
python -m modules.pipeline concalls TCS INFY

# Update technicals
python -m modules.pipeline technicals

# Run scoring for all
python -m modules.pipeline scoring

# Full pipeline
python -m modules.pipeline full
```

---

## Expanding from Nifty 50 → 200 → 500

1. Add company entries to `data/nifty200.json` (same format as nifty50.json)
2. Set `index_membership: ["nifty200"]` for new companies
3. Run seed + full pipeline for new companies only

The leaderboard index filter (`nifty50`, `nifty200`, `nifty500`) will automatically show the right universe.

---

## Walk-the-talk engine

The S5/S6 scoring works in two phases:

**Phase 1 (concall parse):** Claude AI extracts guidance from each concall PDF into `re_guidance` with `hit_miss_score = NULL`.

**Phase 2 (scoring):** The `link_guidance_to_actuals()` function in `scoring.py` matches each guidance row to actual financials for that period and computes `hit_miss_score` (-2 to +2).

**Scoring logic (direction + magnitude):**
- Guided "strong growth" + actual ≥ 18% YoY → major beat (+2)
- Guided "strong growth" + actual 10–18% → met (+1)
- Guided "strong growth" + actual 3–10% → minor miss (-1)
- Guided "strong growth" + actual < 3% → major miss (-2)
- Quantified guidance: ±3% band = met, >3% over = beat, >3% under = miss

---

## Database tables

All tables use `re_` prefix in Supabase project `aiebaqvclyzxajigvkfd`.

| Table | Purpose |
|---|---|
| `re_companies` | Company master (sector, BSE code, index membership) |
| `re_financials` | Quarterly P&L (revenue, EBITDA, PAT, EPS, OPM) |
| `re_concall` | Concall PDF text + Claude AI output |
| `re_guidance` | Guidance given per quarter + actual vs guided |
| `re_scores` | All 10 parameter scores + composite per quarter |
| `re_technical` | Daily RS, DMA, 52W rank, momentum score |
| `re_pipeline_logs` | Run history and error tracking |
| `re_config` | Key-value config (active quarter, model names) |
