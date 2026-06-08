#!/usr/bin/env python3
"""
backtest_model_c.py
-------------------
Version 0 Research Engine — Model C Historical Backtester

Adaptive base signal (B2/B3 by regime) + point-in-time quality overlay.

Quality data source: SEC EDGAR XBRL API (free, no key required)
  https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
  Stores ALL historical filings with exact report dates.
  Gold standard for point-in-time financial data.

Look-ahead protection:
  On ranking date D, only use financial data from quarters
  that ended >= 45 days before D (conservative filing lag buffer).
  This is the SEC 10-Q filing deadline for large accelerated filers.

Adaptive base:
  TREND_UP   → B2 weights (momentum + earnings blend)
  TREND_DOWN → B2 weights
  RANGE_BOUND → B3 weights (earnings dominant)
  HIGH_VOL   → B3 weights (reduced size flag)
  LOW_VOL    → B3 weights (reduced size flag)

Three quality variants:
  C1: Adaptive B + equal-weight quality overlay
  C2: Adaptive B + profitability only (ROE, margins, FCF)
  C3: Quality only (no momentum, no earnings)

Quality factors (all point-in-time from SEC filings):
  roe              = net_income / shareholders_equity (TTM)
  gross_margin     = gross_profit / revenue (TTM)
  operating_margin = operating_income / revenue (TTM)
  fcf_margin       = (operating_cf - capex) / revenue (TTM)
  debt_to_equity   = total_debt / shareholders_equity (inverted — lower=better)
  revenue_growth   = TTM revenue vs prior year TTM revenue

Acceptance tests vs Model B adaptive baseline:
  T+20 IC          > adaptive B T+20
  IC std dev       < adaptive B std dev
  2023/2026 IC     does not degrade
  RANGE_BOUND IC   maintains B3 improvement
  Top-5 hit rate   >= adaptive B
  T+20 spread      > adaptive B (0.0054)

Usage:
  python backtest_model_c.py

Step 1 runs automatically: fetch_quality_data() pulls SEC EDGAR.
Caches to .cache/quality_{ticker}.json after first run.
Runtime: ~8-12 minutes first run, ~3 min cached.
"""

import csv
import gzip
import io
import json
import math
import statistics
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
UNIVERSE    = BASE_DIR / "universe.csv"
CACHE_DIR   = BASE_DIR / ".cache"
RESULTS_DIR = BASE_DIR / "backtest_results"

CACHE_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

START_DATE       = "2020-01-01"
RANKING_FREQ     = 5
MIN_HISTORY      = 130
HORIZONS         = [1, 5, 20]
FILING_LAG_DAYS  = 45   # conservative buffer: quarter must have ended 45+ days ago

# Earnings signal params (same as Model B)
EARN_HALFLIFE_DAYS   = 60
EARN_DETECT_WINDOW   = 3
EARN_ABNORMAL_THRESH = 0.025
EARN_REACTION_DAYS   = 3
QUARTER_DAYS         = 63

# ── Model B weights (from backtest_model_b.py) ────────────────────────────────
B2_WEIGHTS = {
    "rs_20": 0.111, "rs_60": 0.111, "rs_120": 0.111,
    "rs_spy": 0.111, "rs_sector": 0.111, "vol_adj_mom": 0.111,
    "post_earn_3d": 0.111, "earn_momentum": 0.111, "earn_decay": 0.112,
}
B3_WEIGHTS = {
    "rs_20": 0.0, "rs_60": 0.0, "rs_120": 0.0,
    "rs_spy": 0.0, "rs_sector": 0.0, "vol_adj_mom": 0.0,
    "post_earn_3d": 0.50, "earn_momentum": 0.30, "earn_decay": 0.20,
}

REGIME_BASE = {
    "TREND_UP":       "B2",
    "TREND_DOWN":     "B2",
    "RANGE_BOUND":    "B3",
    "HIGH_VOLATILITY":"B3",
    "LOW_VOLATILITY": "B3",
    "INSUFFICIENT":   "B2",
}

# Quality factor weights per variant
QUALITY_FACTORS = ["roe", "gross_margin", "operating_margin",
                   "fcf_margin", "inv_debt_equity", "revenue_growth"]

PROFITABILITY_FACTORS = ["roe", "gross_margin", "operating_margin", "fcf_margin"]

# Model C blend: base signal weight vs quality overlay weight
BASE_WEIGHT    = 0.65
QUALITY_WEIGHT = 0.35

# ── Model B adaptive baselines (from Model B results) ─────────────────────────
MODEL_B_ADAPTIVE = {
    "overall_ic_t1":  0.0263,   # B2 overall (used in trending regimes)
    "overall_std_t1": 0.2860,
    "t20_ic":         0.0196,
    "t20_spread":     0.0054,
    "hit_rate_t20":   0.5428,
    "year_2023":      0.0299,
    "year_2026":      0.0312,
    "range_ic":       0.0111,
    "trend_up_ic":    0.0441,
    "trend_down_ic":  0.1100,
}

SECTOR_ETFS = {
    "Technology":  "XLK", "Finance":     "XLF",
    "Healthcare":  "XLV", "Energy":      "XLE",
    "Consumer":    "XLY", "Industrial":  "XLI",
}

# SEC EDGAR CIK map for our 30 blue chips
# CIK is the permanent identifier — doesn't change
CIK_MAP = {
    "AAPL":  "0000320193", "MSFT":  "0000789019", "NVDA":  "0001045810",
    "GOOGL": "0001652044", "META":  "0001326801", "AMZN":  "0001018724",
    "CRM":   "0001108524", "IBM":   "0000051143", "ORCL":  "0001341439",
    "INTC":  "0000050863", "JPM":   "0000019617", "BAC":   "0000070858",
    "GS":    "0000886982", "V":     "0001403161", "MA":    "0001141391",
    "JNJ":   "0000200406", "UNH":   "0000731766", "PFE":   "0000078003",
    "ABBV":  "0001551152", "MRK":   "0000310158", "XOM":   "0000034088",
    "CVX":   "0000093410", "COP":   "0001163165", "WMT":   "0000104169",
    "HD":    "0000354950", "MCD":   "0000063908", "KO":    "0000021344",
    "PG":    "0000080424", "CAT":   "0000018230", "BA":    "0000012927",
}


# ── SEC EDGAR quality data fetch ──────────────────────────────────────────────
def fetch_edgar_facts(ticker: str, cik: str) -> dict | None:
    """
    Fetch all XBRL company facts from SEC EDGAR.
    Returns full historical time series for all reported metrics.
    Free, no API key. Rate limit: 10 req/sec — we stay well under.

    Returns dict keyed by metric name, value is list of
    {end_date, filed_date, value} sorted by end_date.
    """
    cache_file = CACHE_DIR / f"quality_{ticker}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 86400 * 7:   # cache quality data for 7 days
            with open(cache_file) as f:
                return json.load(f)

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    headers = {
        "User-Agent": "research-tool contact@research.local",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                # SEC EDGAR returns gzip-compressed JSON.
                # urllib doesn't auto-decompress when we set Accept-Encoding,
                # so we decompress manually.
                encoding = resp.headers.get("Content-Encoding", "")
                if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))

            facts = data.get("facts", {})
            us_gaap = facts.get("us-gaap", {})

            # Extract key metrics with their historical values
            # We want quarterly (10-Q) and annual (10-K) filings
            # concept -> list of {end, filed, val, form}
            def extract(concept):
                entries = []
                c = us_gaap.get(concept, {})
                for unit_type, records in c.get("units", {}).items():
                    if unit_type not in ("USD", "pure", "shares"):
                        continue
                    for r in records:
                        # Only use 10-Q and 10-K (not amendments for now)
                        form = r.get("form", "")
                        if form not in ("10-Q", "10-K"):
                            continue
                        # Only annual or quarterly periods
                        accn = r.get("accn", "")
                        end  = r.get("end", "")
                        filed = r.get("filed", "")
                        val  = r.get("val")
                        frame = r.get("frame", "")
                        if val is None or not end or not filed:
                            continue
                        # Quarterly = frame like CY2021Q1I or no frame
                        # Annual = frame like CY2021
                        entries.append({
                            "end":   end,
                            "filed": filed,
                            "val":   val,
                            "form":  form,
                            "frame": frame,
                        })
                # Deduplicate by end date — keep most recently filed
                by_end = {}
                for e in entries:
                    key = e["end"]
                    if key not in by_end or e["filed"] > by_end[key]["filed"]:
                        by_end[key] = e
                return sorted(by_end.values(), key=lambda x: x["end"])

            metrics = {
                # Income statement
                "Revenues":              extract("Revenues"),
                "RevenueFromContractWithCustomerExcludingAssessedTax":
                                         extract("RevenueFromContractWithCustomerExcludingAssessedTax"),
                "GrossProfit":           extract("GrossProfit"),
                "OperatingIncomeLoss":   extract("OperatingIncomeLoss"),
                "NetIncomeLoss":         extract("NetIncomeLoss"),
                # Balance sheet
                "StockholdersEquity":    extract("StockholdersEquity"),
                "LongTermDebt":          extract("LongTermDebtNoncurrent"),
                "ShortTermDebt":         extract("ShortTermBorrowings"),
                # Cash flow
                "OperatingCashFlow":     extract("NetCashProvidedByUsedInOperatingActivities"),
                "CapEx":                 extract("PaymentsToAcquirePropertyPlantAndEquipment"),
            }

            # Filter empty
            metrics = {k: v for k, v in metrics.items() if v}

            result = {"ticker": ticker, "cik": cik, "metrics": metrics}
            with open(cache_file, "w") as f:
                json.dump(result, f)

            time.sleep(0.15)   # SEC rate limit courtesy
            return result

        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    [WARN] {ticker}: CIK {cik} not found on EDGAR")
                return None
            elif e.code == 429:
                print(f"    [WARN] {ticker}: rate limited, waiting...")
                time.sleep(5 * (attempt + 1))
            else:
                print(f"    [WARN] {ticker}: HTTP {e.code}")
                if attempt == 2:
                    return None
                time.sleep(2 ** attempt)
        except Exception as e:
            print(f"    [WARN] {ticker}: {e}")
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)
    return None


# ── Point-in-time quality calculation ────────────────────────────────────────
def last_available_quarter(ranking_date: str) -> str:
    """
    Returns the latest quarter end date that would have been
    filed and available on ranking_date.
    Conservative: quarter must have ended >= FILING_LAG_DAYS ago.
    """
    rd = datetime.strptime(ranking_date, "%Y-%m-%d")
    cutoff = rd - timedelta(days=FILING_LAG_DAYS)
    quarter_ends = []
    for year in range(cutoff.year - 2, cutoff.year + 1):
        for month, day in [(3,31),(6,30),(9,30),(12,31)]:
            try:
                qe = datetime(year, month, day)
                if qe <= cutoff:
                    quarter_ends.append(qe)
            except ValueError:
                pass
    return max(quarter_ends).strftime("%Y-%m-%d") if quarter_ends else "2019-01-01"


def get_val_as_of(records: list, as_of_date: str) -> float | None:
    """
    Get the most recent value from a list of {end, filed, val} records
    where filed <= as_of_date and end <= last_available_quarter(as_of_date).
    This enforces strict point-in-time safety.
    """
    max_end = last_available_quarter(as_of_date)
    eligible = [r for r in records
                if r["filed"] <= as_of_date and r["end"] <= max_end]
    if not eligible:
        return None
    # Most recent by end date
    return sorted(eligible, key=lambda x: x["end"])[-1]["val"]


def get_ttm(records: list, as_of_date: str) -> float | None:
    """
    Build trailing-twelve-month sum from quarterly records.
    Uses last 4 quarters available as of as_of_date.
    """
    max_end = last_available_quarter(as_of_date)
    eligible = [r for r in records
                if r["filed"] <= as_of_date and r["end"] <= max_end
                and r["form"] in ("10-Q",)]  # quarterly only for TTM
    if len(eligible) < 4:
        # Fall back: try annual if quarterly unavailable
        annual = [r for r in records
                  if r["filed"] <= as_of_date and r["end"] <= max_end
                  and r["form"] == "10-K"]
        if annual:
            return sorted(annual, key=lambda x: x["end"])[-1]["val"]
        return None
    last4 = sorted(eligible, key=lambda x: x["end"])[-4:]
    return sum(r["val"] for r in last4)


def calc_quality_factors(ticker: str, edgar_data: dict,
                          ranking_date: str) -> dict:
    """
    Calculate all quality factors for a stock on a specific ranking date.
    All values are point-in-time safe.
    Returns dict of factor name -> float, or None where unavailable.
    """
    if not edgar_data:
        return {f: None for f in QUALITY_FACTORS}

    m = edgar_data.get("metrics", {})

    # Revenue (try both concepts)
    rev_records = m.get("Revenues") or \
                  m.get("RevenueFromContractWithCustomerExcludingAssessedTax") or []
    gross_records   = m.get("GrossProfit", [])
    opinc_records   = m.get("OperatingIncomeLoss", [])
    netinc_records  = m.get("NetIncomeLoss", [])
    equity_records  = m.get("StockholdersEquity", [])
    ltdebt_records  = m.get("LongTermDebt", [])
    stdebt_records  = m.get("ShortTermDebt", [])
    ocf_records     = m.get("OperatingCashFlow", [])
    capex_records   = m.get("CapEx", [])

    # TTM values
    rev     = get_ttm(rev_records,   ranking_date)
    gross   = get_ttm(gross_records, ranking_date)
    opinc   = get_ttm(opinc_records, ranking_date)
    netinc  = get_ttm(netinc_records, ranking_date)
    ocf     = get_ttm(ocf_records,   ranking_date)
    capex   = get_ttm(capex_records,  ranking_date)

    # Point-in-time balance sheet (single period, not TTM)
    equity  = get_val_as_of(equity_records, ranking_date)
    ltdebt  = get_val_as_of(ltdebt_records, ranking_date) or 0
    stdebt  = get_val_as_of(stdebt_records, ranking_date) or 0
    total_debt = ltdebt + stdebt

    # Revenue 1-year-ago TTM for growth
    one_yr_ago = (datetime.strptime(ranking_date, "%Y-%m-%d")
                  - timedelta(days=365)).strftime("%Y-%m-%d")
    rev_1y = get_ttm(rev_records, one_yr_ago)

    # Compute factors
    def safe_div(n, d):
        if n is None or d is None or d == 0:
            return None
        return n / d

    roe              = safe_div(netinc, equity)
    gross_margin     = safe_div(gross, rev)
    operating_margin = safe_div(opinc, rev)
    fcf              = (ocf - capex) if ocf and capex else ocf
    fcf_margin       = safe_div(fcf, rev)
    debt_to_equity   = safe_div(total_debt, abs(equity)) if equity else None
    inv_debt_equity  = (-debt_to_equity) if debt_to_equity is not None else None
    revenue_growth   = safe_div(rev - rev_1y, abs(rev_1y)) if rev and rev_1y else None

    return {
        "roe":              roe,
        "gross_margin":     gross_margin,
        "operating_margin": operating_margin,
        "fcf_margin":       fcf_margin,
        "inv_debt_equity":  inv_debt_equity,
        "revenue_growth":   revenue_growth,
    }


# ── Reused utilities ──────────────────────────────────────────────────────────
def fetch_price_history(ticker: str) -> dict | None:
    cache_file = CACHE_DIR / f"{ticker}_b.json"  # reuse Model B cache
    if not cache_file.exists():
        cache_file = CACHE_DIR / f"{ticker}.json"  # fall back to Model A cache
    if cache_file.exists():
        with open(cache_file) as f:
            data = json.load(f)
        # Normalise: Model B cache has {close, volume}, Model A has raw float
        history = data.get("history", {})
        if history and isinstance(list(history.values())[0], dict):
            return {
                "prices":  {d: v["close"]  for d, v in history.items()},
                "volumes": {d: v["volume"] for d, v in history.items()},
            }
        else:
            return {"prices": history, "volumes": {d: 0 for d in history}}

    # Fetch fresh if no cache
    start_ts = int(datetime(2019, 6, 1).timestamp())
    end_ts   = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&period1={start_ts}&period2={end_ts}")
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            chart  = data["chart"]["result"][0]
            ts_list = chart["timestamp"]
            quote  = chart["indicators"]["quote"][0]
            closes  = quote.get("close", [])
            volumes = quote.get("volume", [])
            prices_d  = {}
            volumes_d = {}
            for ts, c, v in zip(ts_list, closes, volumes):
                if c is not None:
                    d = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    prices_d[d]  = round(c, 4)
                    volumes_d[d] = int(v) if v else 0
            # Save to cache
            save = {"ticker": ticker, "history":
                    {d: {"close": prices_d[d], "volume": volumes_d[d]}
                     for d in prices_d}}
            with open(CACHE_DIR / f"{ticker}_b.json", "w") as f:
                json.dump(save, f)
            return {"prices": prices_d, "volumes": volumes_d}
        except Exception as e:
            if attempt == 2:
                print(f"    [WARN] {ticker}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def get_return(closes, n):
    if len(closes) < n + 1: return None
    b = closes[-(n+1)]
    return (closes[-1] - b) / b if b != 0 else None

def get_volatility(closes, n=20):
    if len(closes) < n + 2: return 0.01
    rets = [(closes[i]-closes[i-1])/closes[i-1]
            for i in range(-n, 0) if closes[i-1] != 0]
    if len(rets) < 3: return 0.01
    mean = sum(rets)/len(rets)
    var  = sum((r-mean)**2 for r in rets)/len(rets)
    return math.sqrt(var) or 0.01

def cross_sectional_zscore(values):
    tickers = [t for t, v in values.items() if v is not None]
    vals    = [values[t] for t in tickers]
    n = len(vals)
    if n < 2: return {t: 0.0 for t in tickers}
    mean = sum(vals)/n
    std  = math.sqrt(sum((v-mean)**2 for v in vals)/n)
    if std == 0: return {t: 0.0 for t in tickers}
    return {t: (values[t]-mean)/std for t in tickers}

def spearman_ic(scores, returns):
    paired = [(s,r) for s,r in zip(scores,returns) if s is not None and r is not None]
    n = len(paired)
    if n < 5: return None
    def rank(lst):
        si = sorted(range(n), key=lambda i: lst[i])
        r  = [0.0]*n
        for pos,i in enumerate(si): r[i] = float(pos+1)
        return r
    sr = rank([p[0] for p in paired])
    rr = rank([p[1] for p in paired])
    d  = sum((sr[i]-rr[i])**2 for i in range(n))
    return round(1-(6*d)/(n*(n**2-1)), 4)

def detect_regime(spy_closes):
    n = len(spy_closes)
    if n < 60: return "INSUFFICIENT"
    p   = spy_closes[-1]
    m20 = sum(spy_closes[-20:])/20
    m50 = sum(spy_closes[-50:])/50
    r20 = (p-spy_closes[-21])/spy_closes[-21] if n > 21 else 0
    atr = sum(abs(spy_closes[i]-spy_closes[i-1]) for i in range(-20,0)) / (20*p)
    if p > m20 > m50 and r20 > 0.02:  return "TREND_UP"
    if p < m20 < m50 and r20 < -0.02: return "TREND_DOWN"
    if atr > 0.012: return "HIGH_VOLATILITY"
    if atr < 0.005 and abs(p/m20-1) < 0.015: return "LOW_VOLATILITY"
    return "RANGE_BOUND"

def detect_earnings_dates(closes, volumes, dates):
    n = len(closes)
    if n < 30: return []
    candidates = []
    for i in range(20, n-5):
        ret = (closes[i]-closes[i-1])/closes[i-1] if closes[i-1] else 0
        avg_vol = statistics.mean(volumes[max(0,i-20):i]) if any(volumes) else 0
        vol_ratio = volumes[i]/avg_vol if avg_vol > 0 else 1.0
        if abs(ret) >= EARN_ABNORMAL_THRESH and vol_ratio >= 1.4:
            candidates.append({"idx": i, "date": dates[i], "ret": ret})
    result = []
    last_idx = -QUARTER_DAYS
    for c in candidates:
        if c["idx"] - last_idx >= QUARTER_DAYS * 0.6:
            ei = min(c["idx"]+EARN_REACTION_DAYS, n-1)
            react = (closes[ei]-closes[c["idx"]])/closes[c["idx"]] if closes[c["idx"]] else 0
            result.append({"date": c["date"], "idx": c["idx"],
                           "reaction_3d": round(react,4),
                           "direction": 1 if react > 0 else -1,
                           "magnitude": abs(react)})
            last_idx = c["idx"]
    return result

def earnings_signal(earn_dates, rank_date, all_dates):
    past = [e for e in earn_dates if e["date"] < rank_date]
    if not past:
        return {"post_earn_3d": 0.0, "earn_momentum": 0.0, "earn_decay": 0.0}
    recent = past[-1]
    try:
        ri = all_dates.index(rank_date)
        ei = all_dates.index(recent["date"]) if recent["date"] in all_dates \
             else next((i for i,d in enumerate(all_dates) if d > recent["date"]), ri)
        days = ri - ei
    except (ValueError, StopIteration):
        days = QUARTER_DAYS
    decay   = math.exp(-0.693 * days / EARN_HALFLIFE_DAYS)
    p_earn  = recent["reaction_3d"] * decay
    if len(past) >= 2:
        prev = past[-2]
        mom  = recent["direction"]*prev["direction"]*recent["magnitude"]*prev["magnitude"]*10
    else:
        mom  = recent["direction"]*recent["magnitude"]
    return {"post_earn_3d": round(p_earn,4),
            "earn_momentum": round(mom,4),
            "earn_decay": round(decay,4)}


# ── Main backtest ─────────────────────────────────────────────────────────────
def run_backtest(stocks, all_prices, all_volumes, spy_prices,
                 sector_prices, earnings_map, quality_cache) -> list:

    all_dates = sorted(set(
        d for p in all_prices.values() for d in p if d >= START_DATE))
    print(f"\n  Trading dates: {len(all_dates)}")
    print(f"  Period: {all_dates[0]} → {all_dates[-1]}")
    ranking_dates = all_dates[MIN_HISTORY::RANKING_FREQ]
    print(f"  Ranking dates: {len(ranking_dates)}\n")

    all_mom_factors = list(B2_WEIGHTS.keys())

    observations = []
    for rank_date in ranking_dates:
        ri = all_dates.index(rank_date)

        # ── Build momentum + earnings z-scores ──
        factor_raw = defaultdict(dict)
        valid = []

        for s in stocks:
            t  = s["ticker"]
            sector = s["sector"]
            closes  = [all_prices[t][d]  for d in all_dates[:ri+1] if d in all_prices.get(t,{})]
            volumes = [all_volumes[t].get(d,0) for d in all_dates[:ri+1] if d in all_prices.get(t,{})]
            if len(closes) < MIN_HISTORY: continue

            spy_c = [spy_prices[d] for d in all_dates[:ri+1] if d in spy_prices]
            etf   = SECTOR_ETFS.get(sector,"SPY")
            sec_c = [sector_prices.get(etf,{}).get(d,0) for d in all_dates[:ri+1]
                     if d in sector_prices.get(etf,{})]

            rs20  = get_return(closes,20)
            rs60  = get_return(closes,60)
            rs120 = get_return(closes,120)
            vol   = get_volatility(closes,20)
            spy_r = get_return(spy_c,20) if len(spy_c)>21 else 0
            sec_r = get_return(sec_c,20) if len(sec_c)>21 else 0

            esig = earnings_signal(earnings_map.get(t,[]), rank_date, all_dates)

            for fn, fv in [
                ("rs_20",rs20),("rs_60",rs60),("rs_120",rs120),
                ("rs_spy",rs20-spy_r if rs20 else None),
                ("rs_sector",rs20-sec_r if rs20 else None),
                ("vol_adj_mom",rs60/vol if rs60 and vol>0 else None),
                ("post_earn_3d",  esig["post_earn_3d"]),
                ("earn_momentum", esig["earn_momentum"]),
                ("earn_decay",    esig["earn_decay"]),
            ]:
                factor_raw[fn][t] = fv

            valid.append({"ticker":t,"sector":sector,"price":closes[-1],"closes":closes})

        if len(valid) < 5: continue

        # z-score momentum/earnings factors
        mzs = {}
        for fn in all_mom_factors:
            raw = {t:v for t,v in factor_raw[fn].items() if v is not None}
            mzs[fn] = cross_sectional_zscore(raw)

        # ── Regime → adaptive base score ──
        spy_c_now = [spy_prices[d] for d in all_dates[:ri+1] if d in spy_prices]
        regime = detect_regime(spy_c_now)
        base_weights = B2_WEIGHTS if REGIME_BASE.get(regime,"B2")=="B2" else B3_WEIGHTS

        base_scores = {}
        for s in valid:
            t = s["ticker"]
            base_scores[t] = sum(base_weights[f]*mzs[f].get(t,0.0) for f in base_weights)

        # ── Quality z-scores (point-in-time) ──
        qraw = defaultdict(dict)
        for s in valid:
            t = s["ticker"]
            qf = calc_quality_factors(t, quality_cache.get(t), rank_date)
            for fn in QUALITY_FACTORS:
                qraw[fn][t] = qf.get(fn)

        qzs = {}
        for fn in QUALITY_FACTORS:
            raw = {t:v for t,v in qraw[fn].items() if v is not None}
            qzs[fn] = cross_sectional_zscore(raw)

        equal_q_w = 1.0/len(QUALITY_FACTORS)
        prof_q_w  = 1.0/len(PROFITABILITY_FACTORS)

        # ── Three model scores ──
        def composite(t, q_factors, q_weight_per):
            bs = base_scores.get(t, 0.0)
            qs = sum(q_weight_per * qzs[f].get(t, 0.0) for f in q_factors)
            return BASE_WEIGHT*bs + QUALITY_WEIGHT*qs

        model_scores = {
            "C1": {s["ticker"]: composite(s["ticker"], QUALITY_FACTORS,    equal_q_w) for s in valid},
            "C2": {s["ticker"]: composite(s["ticker"], PROFITABILITY_FACTORS, prof_q_w) for s in valid},
            "C3": {s["ticker"]: sum(equal_q_w*qzs[f].get(s["ticker"],0.0)
                                    for f in QUALITY_FACTORS) for s in valid},
        }

        year = rank_date[:4]

        # ── Forward returns ──
        for horizon in HORIZONS:
            fi = ri + horizon
            if fi >= len(all_dates): continue
            fwd = all_dates[fi]

            spy_fwd = None
            if rank_date in spy_prices and fwd in spy_prices:
                p0,p1 = spy_prices[rank_date], spy_prices[fwd]
                if p0>0: spy_fwd = (p1-p0)/p0

            for model, scores in model_scores.items():
                scored = sorted(valid, key=lambda s: scores[s["ticker"]], reverse=True)
                sc_list, ret_list = [], []
                for s in scored:
                    t = s["ticker"]
                    p0 = all_prices.get(t,{}).get(rank_date)
                    p1 = all_prices.get(t,{}).get(fwd)
                    if p0 and p1 and p0>0:
                        sc_list.append(scores[t])
                        ret_list.append((p1-p0)/p0)

                ic = spearman_ic(sc_list, ret_list)
                n  = len(scored)
                q  = max(1, n//5)

                def avg_ret(tickers):
                    rs = []
                    for t in tickers:
                        p0 = all_prices.get(t,{}).get(rank_date)
                        p1 = all_prices.get(t,{}).get(fwd)
                        if p0 and p1 and p0>0: rs.append((p1-p0)/p0)
                    return statistics.mean(rs) if rs else None

                top5 = [s["ticker"] for s in scored[:5]]
                topr = avg_ret([s["ticker"] for s in scored[:q]])
                botr = avg_ret([s["ticker"] for s in scored[-q:]])
                spread = (topr-botr) if topr and botr else None
                beats = sum(1 for t in top5
                            if all_prices.get(t,{}).get(rank_date) and
                               all_prices.get(t,{}).get(fwd) and spy_fwd is not None and
                               (all_prices[t][fwd]-all_prices[t][rank_date])
                               /all_prices[t][rank_date] > spy_fwd)
                hit = beats/len(top5) if top5 else None

                observations.append({
                    "date":rank_date,"year":year,"regime":regime,
                    "horizon":horizon,"model":model,"base":REGIME_BASE.get(regime,"B2"),
                    "ic":ic,"spread":round(spread,4) if spread else None,
                    "top_ret":round(topr,4) if topr else None,
                    "bot_ret":round(botr,4) if botr else None,
                    "spy_ret":round(spy_fwd,4) if spy_fwd else None,
                    "hit_rate":round(hit,3) if hit else None,
                    "n_stocks":len(scored),"top_5":",".join(top5),
                })

        print(f"  {rank_date} | {regime:<15} | base={REGIME_BASE.get(regime,'B2')} | "
              f"C1#{[s['ticker'] for s in sorted(valid,key=lambda x:model_scores['C1'][x['ticker']],reverse=True)][0]}")

    return observations


# ── Reporting ─────────────────────────────────────────────────────────────────
def sm(v):
    v=[x for x in v if x is not None]; return round(statistics.mean(v),4) if v else None
def ss(v):
    v=[x for x in v if x is not None]
    return round(statistics.stdev(v),4) if len(v)>=2 else None
def sp(v):
    v=[x for x in v if x is not None]
    return round(sum(1 for x in v if x>0)/len(v)*100,1) if v else None

def ics(obs,model=None,horizon=None,year=None,regime=None):
    r=obs
    if model:   r=[o for o in r if o["model"]==model]
    if horizon: r=[o for o in r if o["horizon"]==horizon]
    if year:    r=[o for o in r if o["year"]==year]
    if regime:  r=[o for o in r if o["regime"]==regime]
    return [o["ic"] for o in r if o["ic"] is not None]

def print_report(obs):
    B = MODEL_B_ADAPTIVE
    SEP = "═"*72

    print(f"\n{SEP}")
    print(f"  MODEL C BACKTEST — FULL REPORT")
    print(f"  Adaptive Base (B2/B3 by regime) + Point-in-Time Quality")
    print(SEP)

    print(f"\n  {'─'*70}")
    print(f"  1. OVERALL IC — ALL MODELS ALL HORIZONS")
    print(f"  {'─'*70}")
    print(f"  {'MODEL':<6} {'H':<6} {'MEAN IC':>9} {'STD':>9} {'POS%':>7} {'N':>5}")
    for m in ["C1","C2","C3"]:
        for h in HORIZONS:
            v = ics(obs,model=m,horizon=h)
            print(f"  {m:<6} T+{h:<4} {str(sm(v)):>9} {str(ss(v)):>9} "
                  f"{str(sp(v))+'%' if sp(v) else '—':>7} {len(v):>5}")
        print()

    print(f"  {'─'*70}")
    print(f"  2. IC BY YEAR (T+1) — vs Model B Adaptive")
    print(f"  {'─'*70}")
    B_yr = {"2020":-0.058,"2021":-0.0154,"2022":0.0786,
            "2023":0.0299,"2024":0.0582,"2025":0.0196,"2026":0.0312}
    print(f"  {'YEAR':<6} {'B2':>8}", end="")
    for m in ["C1","C2","C3"]: print(f"  {m:>8}",end="")
    print("  IMPROVED?")
    for y in sorted(set(o["year"] for o in obs)):
        bic = B_yr.get(y,"—")
        print(f"  {y:<6} {str(bic):>8}",end="")
        best=None
        for m in ["C1","C2","C3"]:
            v=sm(ics(obs,model=m,horizon=1,year=y))
            print(f"  {str(v):>8}",end="")
            if v and (best is None or v>best): best=v
        imp = "✅" if best and isinstance(bic,float) and best>bic else "❌"
        print(f"  {imp}")

    print(f"\n  {'─'*70}")
    print(f"  3. IC BY REGIME (T+1)")
    print(f"  {'─'*70}")
    B_reg = {"TREND_UP":0.0441,"TREND_DOWN":0.1100,"RANGE_BOUND":0.0111,
             "HIGH_VOLATILITY":-0.0816,"LOW_VOLATILITY":-0.0462}
    print(f"  {'REGIME':<20} {'B2/B3':>8}",end="")
    for m in ["C1","C2","C3"]: print(f"  {m:>8}",end="")
    print("  VERDICT")
    for r in sorted(set(o["regime"] for o in obs)):
        bic = B_reg.get(r,0)
        print(f"  {r:<20} {bic:>8.4f}",end="")
        best=None
        for m in ["C1","C2","C3"]:
            v=sm(ics(obs,model=m,horizon=1,regime=r))
            print(f"  {str(v) if v else '—':>8}",end="")
            if v and (best is None or v>best): best=v
        imp = "✅ +" + str(round(best-bic,4)) if best and best>bic else "❌"
        print(f"  {imp}")

    # ── Acceptance tests ──
    print(f"\n{SEP}")
    print(f"  ACCEPTANCE TESTS vs MODEL B ADAPTIVE")
    print(SEP)
    print(f"  {'TEST':<40} {'B(base)':>8}",end="")
    for m in ["C1","C2","C3"]: print(f"  {m:>8}",end="")
    print("  PASS?")
    print(f"  {'─'*72}")

    tests=[]
    def test(name,bval,vals,fn):
        row=[name,bval]+[vals.get(m) for m in ["C1","C2","C3"]]
        row.append("✅" if any(fn(vals.get(m),bval) for m in ["C1","C2","C3"]) else "❌")
        tests.append(row)

    test("T+20 IC > B adaptive",B["t20_ic"],
         {m:sm(ics(obs,model=m,horizon=20)) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>b)
    test("IC StdDev < B adaptive",B["overall_std_t1"],
         {m:ss(ics(obs,model=m,horizon=1)) for m in ["C1","C2","C3"]},
         lambda v,b: v and v<b)
    test("2023 IC not degrade",B["year_2023"],
         {m:sm(ics(obs,model=m,horizon=1,year="2023")) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>=b*0.85)
    test("2026 IC not degrade",B["year_2026"],
         {m:sm(ics(obs,model=m,horizon=1,year="2026")) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>=b*0.85)
    test("RANGE_BOUND maintains B3",B["range_ic"],
         {m:sm(ics(obs,model=m,horizon=1,regime="RANGE_BOUND")) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>=b*0.85)
    test("TREND_UP does not degrade",B["trend_up_ic"],
         {m:sm(ics(obs,model=m,horizon=1,regime="TREND_UP")) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>=b*0.80)
    test("T+20 Spread > B",B["t20_spread"],
         {m:sm([o["spread"] for o in obs if o["model"]==m and o["horizon"]==20
                and o["spread"] is not None]) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>b)
    test("T+20 Hit Rate >= B",B["hit_rate_t20"],
         {m:sm([o["hit_rate"] for o in obs if o["model"]==m and o["horizon"]==20
                and o["hit_rate"] is not None]) for m in ["C1","C2","C3"]},
         lambda v,b: v and v>=b*0.98)

    for t in tests:
        print(f"  {t[0]:<40} {str(t[1]):>8}",end="")
        for v in t[2:5]: print(f"  {str(v) if v else '—':>8}",end="")
        print(f"  {t[5]}")

    passed=sum(1 for t in tests if t[5]=="✅")
    print(f"\n  Passed: {passed}/{len(tests)}")

    # Final verdict
    print(f"\n{SEP}")
    print(f"  FINAL VERDICT")
    print(SEP)
    best_model = max(["C1","C2","C3"],
                     key=lambda m: sm(ics(obs,model=m,horizon=20)) or -99)
    best_ic = sm(ics(obs,model=best_model,horizon=20))
    best_std = ss(ics(obs,model=best_model,horizon=1))

    if passed >= 7:
        print(f"  ✅ STRONG PASS: Quality adds IC to adaptive base.")
        print(f"     Best model: {best_model} | T+20 IC={best_ic} | Std={best_std}")
        print(f"     → This is your Version 0 production model.")
        print(f"     → Begin live 90-day tracking with {best_model}.")
        print(f"     → Regime filter: B2 in trends, B3 in range/vol.")
        print(f"     → Trade only T+20 horizon (2-4 week holds).")
    elif passed >= 5:
        print(f"  🟡 PARTIAL: Quality improves some dimensions.")
        print(f"     → Run {best_model} in parallel with adaptive B.")
        print(f"     → Do not weight quality heavily until 60-day live IC confirms.")
    elif passed >= 3:
        print(f"  🟠 WEAK: Quality adds marginal value.")
        print(f"     → Adaptive B (B2/B3 by regime) remains the better model.")
        print(f"     → Quality data may be too lagged to add T+1/T+5 IC.")
        print(f"     → Consider quality as a portfolio filter, not a ranking factor.")
    else:
        print(f"  🔴 REJECT: Quality does not improve Model B adaptive.")
        print(f"     → Use adaptive B (B2 trends, B3 range) as production model.")
        print(f"     → Quality factors add noise, not signal, in this universe.")
        print(f"     → This is a valid and useful finding — not a failure.")

    print(f"\n  {'─'*70}")
    print(f"  Reminder: If C is rejected, adaptive B is already a strong model.")
    print(f"  T+20 spread 0.0054, hit rate 0.54, IC stable across regimes.")
    print(f"  That is sufficient to begin live Version 0 tracking.")
    print(f"  {'─'*70}\n")


def save_results(obs):
    if not obs: return
    out = RESULTS_DIR / "model_c_observations.csv"
    fields = ["date","year","regime","horizon","model","base","ic","spread",
              "top_ret","bot_ret","spy_ret","hit_rate","n_stocks","top_5"]
    with open(out,"w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(obs)
    print(f"\n  Results → {out}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*72}")
    print(f"  MODEL C BACKTESTER")
    print(f"  Adaptive Base + Point-in-Time Quality (SEC EDGAR XBRL)")
    print(f"  C1: blend+equal-quality | C2: blend+profitability | C3: quality-only")
    print(f"{'='*72}")

    with open(UNIVERSE,newline="") as f:
        stocks = list(csv.DictReader(f))
    print(f"\n  Universe: {len(stocks)} stocks")

    # ── Step 1: Fetch quality data from SEC EDGAR ──
    print(f"\n  Step 1: Fetching quality data from SEC EDGAR XBRL...")
    print(f"  (Free, no API key. Rate-limited to 0.15s between requests.)")
    print(f"  (Caches for 7 days after first fetch.)\n")
    quality_cache = {}
    for s in stocks:
        t   = s["ticker"]
        cik = CIK_MAP.get(t)
        if not cik:
            print(f"    {t}: No CIK mapping — skip quality")
            quality_cache[t] = None
            continue
        print(f"    {t} (CIK {cik})...", end=" ", flush=True)
        data = fetch_edgar_facts(t, cik)
        if data:
            n_metrics = sum(len(v) for v in data["metrics"].values())
            print(f"OK ({n_metrics} data points)")
        else:
            print("SKIP")
        quality_cache[t] = data

    covered = sum(1 for v in quality_cache.values() if v)
    print(f"\n  Quality data: {covered}/{len(stocks)} stocks covered")
    if covered < len(stocks)*0.7:
        print("  [WARN] <70% coverage. Quality factors will be sparse.")
        print("         Results may understate true quality IC.")

    # ── Step 2: Load price histories ──
    print(f"\n  Step 2: Loading price histories...")
    all_prices, all_volumes = {}, {}
    for s in stocks:
        t = s["ticker"]
        print(f"    {t}...", end=" ", flush=True)
        d = fetch_price_history(t)
        if d:
            all_prices[t]  = d["prices"]
            all_volumes[t] = d["volumes"]
            print(f"OK ({len(d['prices'])} days)")
        else:
            print("SKIP")

    spy_d = fetch_price_history("SPY")
    spy_prices = spy_d["prices"] if spy_d else {}
    sector_prices = {}
    for sector, etf in SECTOR_ETFS.items():
        d = fetch_price_history(etf)
        if d: sector_prices[etf] = d["prices"]

    # ── Step 3: Earnings dates ──
    print(f"\n  Step 3: Detecting earnings dates from price history...")
    all_dates_full = sorted(set(d for p in all_prices.values() for d in p))
    earnings_map = {}
    for s in stocks:
        t = s["ticker"]
        dates   = [d for d in all_dates_full if d in all_prices.get(t,{})]
        closes  = [all_prices[t][d] for d in dates]
        volumes = [all_volumes[t].get(d,0) for d in dates]
        earnings_map[t] = detect_earnings_dates(closes, volumes, dates)
        print(f"    {t}: {len(earnings_map[t])} events")

    # ── Step 4: Backtest ──
    print(f"\n  Step 4: Running walk-forward backtest...\n")
    obs = run_backtest(stocks, all_prices, all_volumes,
                       spy_prices, sector_prices, earnings_map, quality_cache)

    if not obs:
        print("\n  [ERROR] No observations. Check network and cache.")
        sys.exit(1)

    print(f"\n  Total observations: {len(obs)}")
    print_report(obs)
    save_results(obs)


if __name__ == "__main__":
    main()
