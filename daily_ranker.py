#!/usr/bin/env python3
"""
daily_ranker.py  —  B-Adaptive Production Live Tracker
=======================================================
Version 0 Research Engine

PURPOSE
-------
This is a MEASUREMENT INSTRUMENT, not a signal generator.

Its job for the next 90 days:
  Generate daily B-adaptive rankings and store them with enough
  data to calculate live IC after T+1, T+5, T+20 trading days.

The backtest showed:
  B-adaptive T+20 IC = 0.0196  (mean)
  B-adaptive T+20 std dev = 0.263  (signal-to-noise ratio: 0.074)
  T+20 spread after costs ~0.34%  (thin margin of safety)

This is a HYPOTHESIS, not a proven edge.
90 days of live IC measurement will either confirm or reject it.
Capital deployment decision comes AFTER that confirmation.

ACTIVE MODEL BY REGIME
----------------------
  TREND_UP        → B2  (momentum + earnings blend, equal weight)
  TREND_DOWN      → B2
  RANGE_BOUND     → B3  (earnings dominant)
  HIGH_VOLATILITY → B3  (caution flag — reduced confidence)
  LOW_VOLATILITY  → B3  (caution flag — reduced confidence)

QUALITY VETO
------------
Quality is NOT a ranking factor.
It is a soft veto: bottom-quartile quality names are flagged
and excluded from the top-5 actionable candidates.

NOTE: Live quality data uses current Yahoo fundamentals.
This is point-in-time safe going forward but is a DIFFERENT
data source than the SEC EDGAR data used in the backtest.
Quality veto results are therefore not directly comparable
to backtest quality analysis.

OUTPUT FILES
------------
  live_rankings.csv   — daily rankings with all fields
  prices.csv          — daily closing prices (for IC calculation)
  regime_log.csv      — daily regime log

USAGE
-----
  python daily_ranker.py          # run after market close
  python ic_tracker.py            # run weekly to measure live IC

DO NOT USE THIS OUTPUT TO MAKE TRADING DECISIONS
until 90-day live IC is confirmed positive and stable.
"""

import csv
import json
import math
import statistics
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
UNIVERSE      = BASE_DIR / "universe.csv"
LIVE_RANKINGS = BASE_DIR / "data" / "live_rankings.csv"
PRICES        = BASE_DIR / "data" / "prices.csv"
REGIME_LOG    = BASE_DIR / "data" / "regime_log.csv"

# ── B2 weights — equal weight across all 9 factors ───────────────────────────
# Used in: TREND_UP, TREND_DOWN
B2_WEIGHTS = {
    "rs_20":         0.111,
    "rs_60":         0.111,
    "rs_120":        0.111,
    "rs_spy":        0.111,
    "rs_sector":     0.111,
    "vol_adj_mom":   0.111,
    "post_earn_3d":  0.111,
    "earn_momentum": 0.111,
    "earn_decay":    0.112,
}

# ── B3 weights — earnings dominant ───────────────────────────────────────────
# Used in: RANGE_BOUND, HIGH_VOLATILITY, LOW_VOLATILITY
B3_WEIGHTS = {
    "rs_20":         0.00,
    "rs_60":         0.00,
    "rs_120":        0.00,
    "rs_spy":        0.00,
    "rs_sector":     0.00,
    "vol_adj_mom":   0.00,
    "post_earn_3d":  0.50,
    "earn_momentum": 0.30,
    "earn_decay":    0.20,
}

REGIME_MODEL = {
    "TREND_UP":        ("B2", "NORMAL"),
    "TREND_DOWN":      ("B2", "NORMAL"),
    "RANGE_BOUND":     ("B3", "NORMAL"),
    "HIGH_VOLATILITY": ("B3", "CAUTION — reduced confidence, paper track only"),
    "LOW_VOLATILITY":  ("B3", "CAUTION — reduced confidence, paper track only"),
    "UNKNOWN":         ("B2", "CAUTION — regime unclear"),
}

SECTOR_ETFS = {
    "Technology":  "XLK",
    "Finance":     "XLF",
    "Healthcare":  "XLV",
    "Energy":      "XLE",
    "Consumer":    "XLY",
    "Industrial":  "XLI",
}

# Earnings detection params (same as backtest)
EARN_ABNORMAL_THRESH = 0.025
EARN_HALFLIFE_DAYS   = 60
EARN_REACTION_DAYS   = 3
QUARTER_DAYS         = 63


# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_history(ticker: str, days: int = 130) -> dict | None:
    """
    Fetch daily close + volume history from Yahoo Finance.
    days=130 gives enough history for all factors (rs_120 needs 121 days).
    """
    range_str = "6mo" if days <= 130 else "1y"
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range={range_str}")
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read())
            chart   = data["chart"]["result"][0]
            meta    = chart["meta"]
            ts_list = chart["timestamp"]
            quote   = chart["indicators"]["quote"][0]
            closes  = quote.get("close", [])
            volumes = quote.get("volume", [])
            history = {}
            for ts, c, v in zip(ts_list, closes, volumes):
                if c is not None:
                    d = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    history[d] = {"close": round(c, 4), "volume": int(v) if v else 0}
            return {
                "ticker":  ticker,
                "price":   meta.get("regularMarketPrice", 0),
                "history": history,
            }
        except Exception as e:
            if attempt == 2:
                print(f"    [WARN] {ticker}: {e}")
                return None
            import time; time.sleep(1.5 ** attempt)
    return None


def fetch_quality_veto(ticker: str) -> dict:
    """
    Fetch current quality metrics from Yahoo Finance for veto filter.

    NOTE: This uses CURRENT fundamentals, not point-in-time historical.
    Safe for forward-looking veto decisions but NOT comparable to
    the SEC EDGAR data used in the Model C backtest.

    Returns dict with roe, profit_margin, debt_equity.
    Defaults to neutral (0) on failure — missing data is not a veto.
    """
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
           f"?modules=financialData,defaultKeyStatistics")
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = data["quoteSummary"]["result"][0]
        fin    = result.get("financialData", {})
        stats  = result.get("defaultKeyStatistics", {})
        def val(d, k):
            v = d.get(k, {})
            return v.get("raw", None) if isinstance(v, dict) else v
        return {
            "roe":           val(fin,   "returnOnEquity")   or 0.0,
            "profit_margin": val(fin,   "profitMargins")    or 0.0,
            "debt_equity":   val(stats, "debtToEquity")     or 0.0,
        }
    except Exception:
        return {"roe": 0.0, "profit_margin": 0.0, "debt_equity": 0.0}


# ── Factor calculations ───────────────────────────────────────────────────────
def get_return(closes: list, n: int) -> float | None:
    if len(closes) < n + 1: return None
    b = closes[-(n+1)]
    return (closes[-1] - b) / b if b != 0 else None

def get_volatility(closes: list, n: int = 20) -> float:
    if len(closes) < n + 2: return 0.01
    rets = [(closes[i]-closes[i-1])/closes[i-1]
            for i in range(-n, 0) if closes[i-1] != 0]
    if len(rets) < 3: return 0.01
    mean = sum(rets)/len(rets)
    var  = sum((r-mean)**2 for r in rets)/len(rets)
    return math.sqrt(var) or 0.01

def cross_sectional_zscore(values: dict) -> dict:
    tickers = [t for t, v in values.items() if v is not None]
    vals    = [values[t] for t in tickers]
    n = len(vals)
    if n < 2: return {t: 0.0 for t in tickers}
    mean = sum(vals)/n
    std  = math.sqrt(sum((v-mean)**2 for v in vals)/n)
    if std == 0: return {t: 0.0 for t in tickers}
    return {t: (values[t]-mean)/std for t in tickers}

def quality_composite(roe, profit_margin, debt_equity) -> float:
    """Simple composite for veto filter only. Higher = better quality."""
    debt_pen = min(debt_equity / 100, 2.0) if debt_equity else 0.0
    return (roe * 0.4) + (profit_margin * 0.4) - (debt_pen * 0.2)


# ── Regime detection ──────────────────────────────────────────────────────────
def detect_regime(spy_closes: list) -> str:
    n = len(spy_closes)
    if n < 60: return "UNKNOWN"
    price  = spy_closes[-1]
    ma20   = sum(spy_closes[-20:])/20
    ma50   = sum(spy_closes[-50:])/50
    ret_20 = (price - spy_closes[-21])/spy_closes[-21] if n > 21 else 0
    ranges = [abs(spy_closes[i]-spy_closes[i-1]) for i in range(-20, 0)]
    atr    = (sum(ranges)/20)/price if price > 0 else 0
    if price > ma20 > ma50 and ret_20 > 0.02:   return "TREND_UP"
    if price < ma20 < ma50 and ret_20 < -0.02:  return "TREND_DOWN"
    if atr > 0.012:                              return "HIGH_VOLATILITY"
    if atr < 0.005 and abs(price/ma20-1) < 0.015: return "LOW_VOLATILITY"
    return "RANGE_BOUND"


# ── Earnings signal ───────────────────────────────────────────────────────────
def detect_earnings_dates(closes: list, volumes: list, dates: list) -> list:
    """
    Detect likely earnings dates from abnormal price + volume.
    Same logic as backtest — ensures consistency.
    """
    n = len(closes)
    if n < 30: return []
    candidates = []
    for i in range(20, n-3):
        ret = (closes[i]-closes[i-1])/closes[i-1] if closes[i-1] else 0
        avg_vol = statistics.mean(volumes[max(0,i-20):i]) if any(volumes) else 1
        vol_ratio = volumes[i]/avg_vol if avg_vol > 0 else 1.0
        if abs(ret) >= EARN_ABNORMAL_THRESH and vol_ratio >= 1.4:
            candidates.append({"idx": i, "date": dates[i], "ret": ret})
    result = []
    last_idx = -QUARTER_DAYS
    for c in candidates:
        if c["idx"] - last_idx >= QUARTER_DAYS * 0.6:
            ei = min(c["idx"]+EARN_REACTION_DAYS, n-1)
            react = (closes[ei]-closes[c["idx"]])/closes[c["idx"]] if closes[c["idx"]] else 0
            result.append({
                "date":        c["date"],
                "idx":         c["idx"],
                "reaction_3d": round(react, 4),
                "direction":   1 if react > 0 else -1,
                "magnitude":   abs(react),
            })
            last_idx = c["idx"]
    return result

def earnings_signal(earn_dates: list, today_idx: int, n_dates: int) -> dict:
    """
    Compute earnings factors using only events before today.
    today_idx = position of today in the sorted dates list.
    """
    past = earn_dates  # already filtered to history window
    if not past:
        return {"post_earn_3d": 0.0, "earn_momentum": 0.0, "earn_decay": 0.0}
    recent   = past[-1]
    days_ago = today_idx - recent["idx"]
    decay    = math.exp(-0.693 * days_ago / EARN_HALFLIFE_DAYS)
    p_earn   = recent["reaction_3d"] * decay
    if len(past) >= 2:
        prev = past[-2]
        mom  = (recent["direction"] * prev["direction"]
                * recent["magnitude"] * prev["magnitude"] * 10)
    else:
        mom = recent["direction"] * recent["magnitude"]
    return {
        "post_earn_3d":  round(p_earn, 4),
        "earn_momentum": round(mom, 4),
        "earn_decay":    round(decay, 4),
    }


# ── Main scoring ──────────────────────────────────────────────────────────────
def run_daily(stocks: list) -> tuple[list, str, str, str]:
    """
    Fetch data, compute B-adaptive scores, apply quality veto.
    Returns (ranked_results, regime, active_model, confidence_flag).
    """
    today = datetime.today().strftime("%Y-%m-%d")
    SEP   = "═" * 68

    print(f"\n{SEP}")
    print(f"  B-ADAPTIVE LIVE TRACKER  —  {today}")
    print(f"  Version 0 Research Engine  |  Measurement instrument only")
    print(SEP)
    print(f"\n  Fetching market data...\n")

    # ── SPY for regime ──
    spy_data   = fetch_history("SPY", days=130)
    spy_closes = sorted_closes(spy_data) if spy_data else []
    regime     = detect_regime(spy_closes)
    active_model, confidence = REGIME_MODEL.get(regime, ("B2", "CAUTION"))
    weights = B2_WEIGHTS if active_model == "B2" else B3_WEIGHTS

    print(f"  Regime:       {regime}")
    print(f"  Active model: {active_model}")
    print(f"  Confidence:   {confidence}")
    print()

    # ── Sector ETFs ──
    sector_closes = {}
    for sector, etf in SECTOR_ETFS.items():
        d = fetch_history(etf, days=130)
        if d: sector_closes[sector] = sorted_closes(d)

    # ── Stock data ──
    raw = []
    for s in stocks:
        ticker = s["ticker"]
        print(f"  {ticker:<6}...", end=" ", flush=True)
        data = fetch_history(ticker, days=130)
        if not data:
            print("SKIP")
            continue

        dates_sorted = sorted(data["history"].keys())
        closes  = [data["history"][d]["close"]  for d in dates_sorted]
        volumes = [data["history"][d]["volume"] for d in dates_sorted]
        today_idx = len(closes) - 1

        # ── Momentum factors ──
        rs20    = get_return(closes, 20)
        rs60    = get_return(closes, 60)
        rs120   = get_return(closes, 120)
        vol     = get_volatility(closes, 20)
        spy_r20 = get_return(spy_closes, 20) if len(spy_closes) > 21 else 0
        sec_cls = sector_closes.get(s["sector"], [])
        sec_r20 = get_return(sec_cls, 20)    if len(sec_cls) > 21  else 0

        rs_spy    = (rs20 - spy_r20) if rs20 is not None else None
        rs_sector = (rs20 - sec_r20) if rs20 is not None else None
        vol_adj   = (rs60 / vol)     if rs60 is not None and vol > 0 else None

        # ── Earnings signal ──
        earn_dates = detect_earnings_dates(closes, volumes, dates_sorted)
        esig = earnings_signal(earn_dates, today_idx, len(closes))

        # ── Quality veto data (current fundamentals, forward-safe) ──
        qdata = fetch_quality_veto(ticker)
        qscore = quality_composite(
            qdata["roe"], qdata["profit_margin"], qdata["debt_equity"]
        )

        raw.append({
            "ticker":        ticker,
            "sector":        s["sector"],
            "price":         round(data["price"], 2),
            "rs_20":         rs20,
            "rs_60":         rs60,
            "rs_120":        rs120,
            "rs_spy":        rs_spy,
            "rs_sector":     rs_sector,
            "vol_adj_mom":   vol_adj,
            "post_earn_3d":  esig["post_earn_3d"],
            "earn_momentum": esig["earn_momentum"],
            "earn_decay":    esig["earn_decay"],
            "quality_raw":   round(qscore, 4),
            "earn_events":   len(earn_dates),
        })
        print("OK")

    if not raw:
        print("\n  [ERROR] No data fetched.")
        sys.exit(1)

    # ── Cross-sectional z-scores ──
    all_factors = list(B2_WEIGHTS.keys())
    zscores = {}
    for fn in all_factors:
        vals = {r["ticker"]: r[fn] for r in raw if r[fn] is not None}
        zscores[fn] = cross_sectional_zscore(vals)

    # ── Quality veto threshold: bottom quartile ──
    q_vals = [r["quality_raw"] for r in raw]
    q_thresh = sorted(q_vals)[len(q_vals)//4]  # 25th percentile

    # ── Score and rank ──
    scored = []
    for r in raw:
        t     = r["ticker"]
        score = sum(weights[f] * zscores[f].get(t, 0.0) for f in weights)
        q_pass = r["quality_raw"] > q_thresh
        scored.append({**r, "score": round(score, 4), "quality_pass": q_pass})

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Assign ranks
    results = []
    for rank, s in enumerate(scored, 1):
        results.append({
            "date":          today,
            "regime":        regime,
            "active_model":  active_model,
            "confidence":    confidence,
            "rank":          rank,
            "ticker":        s["ticker"],
            "sector":        s["sector"],
            "price":         s["price"],
            "score":         s["score"],
            "quality_pass":  "PASS" if s["quality_pass"] else "VETO",
            "rs_20":         round(s["rs_20"],   4) if s["rs_20"]   is not None else "",
            "rs_60":         round(s["rs_60"],   4) if s["rs_60"]   is not None else "",
            "rs_120":        round(s["rs_120"],  4) if s["rs_120"]  is not None else "",
            "rs_spy":        round(s["rs_spy"],  4) if s["rs_spy"]  is not None else "",
            "rs_sector":     round(s["rs_sector"],4) if s["rs_sector"] is not None else "",
            "post_earn_3d":  s["post_earn_3d"],
            "earn_momentum": s["earn_momentum"],
            "earn_decay":    s["earn_decay"],
            "earn_events":   s["earn_events"],
            "quality_raw":   s["quality_raw"],
            # Forward returns — filled by ic_tracker.py later
            "ret_t1":  "",
            "ret_t5":  "",
            "ret_t20": "",
        })

    return results, regime, active_model, confidence


def sorted_closes(data: dict) -> list:
    """Extract sorted close prices from fetch_history result."""
    h = data.get("history", {})
    return [h[d]["close"] for d in sorted(h.keys())]


# ── Display ───────────────────────────────────────────────────────────────────
def print_rankings(results: list, confidence: str):
    SEP = "─" * 72
    today    = results[0]["date"]
    regime   = results[0]["regime"]
    model    = results[0]["active_model"]
    is_caution = "CAUTION" in confidence

    print(f"\n{SEP}")
    print(f"  {'RANK':<5} {'TICKER':<7} {'SECTOR':<12} {'SCORE':>7} "
          f"{'QUALITY':>8} {'PRICE':>8}  NOTES")
    print(SEP)

    top5_clean = [r for r in results[:10] if r["quality_pass"] == "PASS"][:5]
    top5_tickers = {r["ticker"] for r in top5_clean}

    for r in results:
        veto_flag = "  ← VETOED" if r["quality_pass"] == "VETO" else ""
        top_flag  = "  ★ TOP CANDIDATE" if r["ticker"] in top5_tickers else ""
        notes     = veto_flag or top_flag or ""
        print(f"  {r['rank']:<5} {r['ticker']:<7} {r['sector']:<12} "
              f"{r['score']:>7.3f} {r['quality_pass']:>8} "
              f"{r['price']:>8.2f}{notes}")

    print(SEP)
    print(f"  Date: {today}  |  Regime: {regime}  |  Model: {model}")
    if is_caution:
        print(f"  ⚠️  {confidence}")
    print(SEP)

    print(f"\n  TOP CANDIDATES (ranked, quality veto applied):")
    print(f"  {'#':<3} {'TICKER':<7} {'SCORE':>7}  EARN_DECAY  POST_EARN_3D")
    print(f"  {'─'*50}")
    for i, r in enumerate(top5_clean, 1):
        print(f"  {i:<3} {r['ticker']:<7} {r['score']:>7.3f}  "
              f"{r['earn_decay']:>9.3f}  {r['post_earn_3d']:>9.4f}")

    if is_caution:
        print(f"\n  ⚠️  CAUTION REGIME: Do not use these rankings for capital deployment.")
        print(f"     Continue paper tracking only. IC in this regime is unreliable.")

    print(f"\n  Reminder: This output is for IC measurement only.")
    print(f"  90-day live IC target must be confirmed before any execution.\n")


# ── Persistence ───────────────────────────────────────────────────────────────
def check_already_run(today: str) -> bool:
    """Prevent duplicate entries for the same day."""
    if not LIVE_RANKINGS.exists():
        return False
    with open(LIVE_RANKINGS, newline="") as f:
        rows = list(csv.DictReader(f))
    return any(r["date"] == today for r in rows)


def save_rankings(results: list):
    fieldnames = [
        "date", "regime", "active_model", "confidence",
        "rank", "ticker", "sector", "price", "score", "quality_pass",
        "rs_20", "rs_60", "rs_120", "rs_spy", "rs_sector",
        "post_earn_3d", "earn_momentum", "earn_decay", "earn_events",
        "quality_raw",
        "ret_t1", "ret_t5", "ret_t20",
    ]
    file_exists = LIVE_RANKINGS.exists()
    with open(LIVE_RANKINGS, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)
    print(f"  Rankings saved  → {LIVE_RANKINGS}")


def save_prices(results: list):
    fieldnames = ["date", "ticker", "price"]
    file_exists = PRICES.exists()
    with open(PRICES, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow({"date": r["date"], "ticker": r["ticker"],
                             "price": r["price"]})
    print(f"  Prices saved    → {PRICES}")


def save_regime(regime: str, model: str, confidence: str, today: str):
    fieldnames = ["date", "regime", "active_model", "confidence"]
    file_exists = REGIME_LOG.exists()
    with open(REGIME_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({"date": today, "regime": regime,
                         "active_model": model, "confidence": confidence})
    print(f"  Regime logged   → {REGIME_LOG}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    today = datetime.today().strftime("%Y-%m-%d")

    # Guard: don't run twice on same day
    if check_already_run(today):
        print(f"\n  Already ran for {today}. Rankings are stored.")
        print(f"  Run ic_tracker.py to see live IC progress.\n")
        return

    # Load universe
    with open(UNIVERSE, newline="") as f:
        stocks = list(csv.DictReader(f))

    # Score
    results, regime, model, confidence = run_daily(stocks)

    # Display
    print_rankings(results, confidence)

    # Save
    save_rankings(results)
    save_prices(results)
    save_regime(regime, model, confidence, today)

    print(f"  Done — {today} logged.")
    print(f"  Run ic_tracker.py weekly to track live IC.\n")


if __name__ == "__main__":
    main()
