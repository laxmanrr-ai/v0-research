#!/usr/bin/env python3
"""
backtest_model_a.py
-------------------
Version 0 Research Engine — Model A Historical Backtester

Tests pure price-momentum ranking across multiple market regimes:
  2020-2021: liquidity/momentum boom
  2022:      bear market / rate shock
  2023-2024: recovery / AI leadership
  2025-2026: current regime

Factors (price-derived only, zero look-ahead bias):
  - 20-day return
  - 60-day return
  - 120-day return
  - RS vs SPY (20d)
  - RS vs sector ETF (20d)
  - volatility-adjusted momentum (momentum / vol)

Key question:
  Do higher-ranked stocks outperform lower-ranked stocks?
  That is what IC measures. Not "did top names go up?"

Output:
  - IC at T+1, T+5, T+20
  - IC by year
  - IC by regime
  - Top-decile vs bottom-decile return spread
  - Hit rate: top 5 ranked names beating SPY

Usage:
  python backtest_model_a.py

No external packages required. Uses Yahoo Finance (free).
Runtime: ~3-5 minutes (network-bound).
"""

import csv
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


# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
UNIVERSE    = BASE_DIR / "universe.csv"
CACHE_DIR   = BASE_DIR / ".cache"
RESULTS_DIR = BASE_DIR / "backtest_results"

CACHE_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# Backtest parameters
START_DATE    = "2020-01-01"
RANKING_FREQ  = 5          # re-rank every N trading days
MIN_HISTORY   = 130        # days of history needed before first ranking
HORIZONS      = [1, 5, 20] # forward return measurement days

# Factor weights for Model A variants
MODEL_A_WEIGHTS = {
    "rs_20":    0.25,
    "rs_60":    0.25,
    "rs_120":   0.20,
    "rs_spy":   0.15,
    "rs_sector":0.10,
    "vol_adj_mom": 0.05,
}

# Sector ETF map
SECTOR_ETFS = {
    "Technology":  "XLK",
    "Finance":     "XLF",
    "Healthcare":  "XLV",
    "Energy":      "XLE",
    "Consumer":    "XLY",
    "Industrial":  "XLI",
}


# ── Data fetching with caching ───────────────────────────────────────────────
def fetch_history(ticker: str, start: str = "2019-06-01") -> dict | None:
    """
    Fetch full daily price history from Yahoo Finance.
    Caches to disk to avoid re-fetching on reruns.
    start: earlier than backtest start to ensure enough warm-up history
    """
    cache_file = CACHE_DIR / f"{ticker}.json"

    # Use cache if fresh (less than 1 day old)
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 86400:
            with open(cache_file) as f:
                return json.load(f)

    start_dt  = datetime.strptime(start, "%Y-%m-%d")
    start_ts  = int(start_dt.timestamp())
    end_ts    = int(time.time())

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={start_ts}&period2={end_ts}"
    )
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            chart = data["chart"]["result"][0]
            timestamps = chart["timestamp"]
            closes     = chart["indicators"]["quote"][0].get("close", [])

            # Build date → close map, skip None values
            history = {}
            for ts, close in zip(timestamps, closes):
                if close is not None:
                    date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    history[date_str] = round(close, 4)

            result = {"ticker": ticker, "history": history}
            with open(cache_file, "w") as f:
                json.dump(result, f)
            return result

        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"    [WARN] {ticker}: {e}")
                return None

    return None


# ── Regime detection ─────────────────────────────────────────────────────────
def detect_regime(spy_closes_to_date: list) -> str:
    """
    Classify market regime using SPY price structure at a point in time.
    Uses only data available on that date (no look-ahead).
    """
    closes = spy_closes_to_date
    n = len(closes)
    if n < 60:
        return "INSUFFICIENT"

    price = closes[-1]
    ma20  = sum(closes[-20:]) / 20
    ma50  = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / 200 if n >= 200 else sum(closes) / n
    ret_20 = (price - closes[-21]) / closes[-21] if n > 21 else 0

    # ATR proxy
    ranges  = [abs(closes[i] - closes[i-1]) for i in range(-20, 0)]
    atr_pct = (sum(ranges) / 20) / price if price > 0 else 0

    if price > ma20 > ma50 and ret_20 > 0.02:
        return "TREND_UP"
    if price < ma20 < ma50 and ret_20 < -0.02:
        return "TREND_DOWN"
    if atr_pct > 0.012:
        return "HIGH_VOLATILITY"
    if atr_pct < 0.005 and abs(price / ma20 - 1) < 0.015:
        return "LOW_VOLATILITY"
    return "RANGE_BOUND"


def year_from_date(date_str: str) -> str:
    return date_str[:4]


# ── Factor calculation ────────────────────────────────────────────────────────
def get_return(closes_list: list, n: int) -> float | None:
    """n-day return. Returns None if insufficient data."""
    if len(closes_list) < n + 1:
        return None
    base = closes_list[-(n + 1)]
    if base == 0:
        return None
    return (closes_list[-1] - base) / base


def get_volatility(closes_list: list, n: int = 20) -> float:
    """n-day return volatility (std dev of daily returns)."""
    if len(closes_list) < n + 2:
        return 0.01
    daily_rets = [
        (closes_list[i] - closes_list[i-1]) / closes_list[i-1]
        for i in range(-n, 0)
        if closes_list[i-1] != 0
    ]
    if len(daily_rets) < 3:
        return 0.01
    mean = sum(daily_rets) / len(daily_rets)
    var  = sum((r - mean) ** 2 for r in daily_rets) / len(daily_rets)
    return math.sqrt(var) or 0.01


def cross_sectional_zscore(values: dict) -> dict:
    """Convert raw factor values to z-scores across all stocks on a given date."""
    tickers = [t for t, v in values.items() if v is not None]
    vals    = [values[t] for t in tickers]
    n = len(vals)
    if n < 2:
        return {t: 0.0 for t in tickers}
    mean = sum(vals) / n
    std  = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
    if std == 0:
        return {t: 0.0 for t in tickers}
    return {t: (values[t] - mean) / std for t in tickers}


# ── Spearman IC ───────────────────────────────────────────────────────────────
def spearman_ic(scores: list, returns: list) -> float | None:
    """Spearman rank correlation between scores and forward returns."""
    paired = [(s, r) for s, r in zip(scores, returns)
              if s is not None and r is not None]
    n = len(paired)
    if n < 5:
        return None

    def rank(lst):
        sorted_idx = sorted(range(n), key=lambda i: lst[i])
        ranks = [0.0] * n
        for r, i in enumerate(sorted_idx):
            ranks[i] = float(r + 1)
        return ranks

    s_vals = [p[0] for p in paired]
    r_vals = [p[1] for p in paired]
    s_ranks = rank(s_vals)
    r_ranks = rank(r_vals)

    d_sq = sum((s_ranks[i] - r_ranks[i]) ** 2 for i in range(n))
    ic   = 1 - (6 * d_sq) / (n * (n**2 - 1))
    return round(ic, 4)


# ── Main backtest engine ──────────────────────────────────────────────────────
def run_backtest(stocks: list, all_prices: dict, spy_prices: dict,
                 sector_prices: dict) -> list:
    """
    Walk forward through time. On each ranking date:
    1. Compute factors using only past data
    2. Score and rank all stocks
    3. Record rankings + regime
    4. Measure forward returns at T+1, T+5, T+20

    Returns list of observation dicts.
    """
    # Build sorted list of all trading dates
    all_dates = sorted(set(
        date for prices in all_prices.values()
        for date in prices
        if date >= START_DATE
    ))

    print(f"\n  Trading dates available: {len(all_dates)}")
    print(f"  Period: {all_dates[0]} → {all_dates[-1]}")

    observations = []
    ranking_dates = all_dates[MIN_HISTORY::RANKING_FREQ]
    print(f"  Ranking dates: {len(ranking_dates)} "
          f"(every {RANKING_FREQ} trading days after {MIN_HISTORY}d warmup)\n")

    for rank_date in ranking_dates:
        rank_idx = all_dates.index(rank_date)

        # ── Build factor values for each stock ──
        factor_raw = defaultdict(dict)
        valid_stocks = []

        for stock in stocks:
            ticker  = stock["ticker"]
            sector  = stock["sector"]
            prices  = all_prices.get(ticker, {})

            # Closes up to and including rank_date
            closes = [prices[d] for d in all_dates[:rank_idx+1]
                      if d in prices]

            if len(closes) < MIN_HISTORY:
                continue

            # SPY closes up to rank_date
            spy_closes = [spy_prices[d] for d in all_dates[:rank_idx+1]
                          if d in spy_prices]

            # Sector ETF closes
            etf = SECTOR_ETFS.get(sector, "SPY")
            sec_prices = sector_prices.get(etf, {})
            sec_closes = [sec_prices[d] for d in all_dates[:rank_idx+1]
                          if d in sec_prices]

            # Raw factors
            rs_20  = get_return(closes, 20)
            rs_60  = get_return(closes, 60)
            rs_120 = get_return(closes, 120)
            vol    = get_volatility(closes, 20)

            spy_ret_20 = get_return(spy_closes, 20) if len(spy_closes) > 21 else 0
            sec_ret_20 = get_return(sec_closes, 20) if len(sec_closes) > 21 else 0

            rs_spy    = (rs_20 - spy_ret_20) if rs_20 is not None else None
            rs_sector = (rs_20 - sec_ret_20) if rs_20 is not None else None
            vol_adj   = (rs_60 / vol) if rs_60 is not None and vol > 0 else None

            factor_raw["rs_20"][ticker]    = rs_20
            factor_raw["rs_60"][ticker]    = rs_60
            factor_raw["rs_120"][ticker]   = rs_120
            factor_raw["rs_spy"][ticker]   = rs_spy
            factor_raw["rs_sector"][ticker] = rs_sector
            factor_raw["vol_adj_mom"][ticker] = vol_adj

            valid_stocks.append({
                "ticker": ticker,
                "sector": sector,
                "price":  closes[-1],
                "closes": closes,
            })

        if len(valid_stocks) < 5:
            continue

        # ── Cross-sectional z-scores ──
        zscores = {}
        for factor in MODEL_A_WEIGHTS:
            raw_vals = {t: v for t, v in factor_raw[factor].items()
                        if v is not None}
            zscores[factor] = cross_sectional_zscore(raw_vals)

        # ── Composite score ──
        scored = []
        for s in valid_stocks:
            t = s["ticker"]
            score = sum(
                MODEL_A_WEIGHTS[f] * zscores[f].get(t, 0.0)
                for f in MODEL_A_WEIGHTS
            )
            scored.append({**s, "score": round(score, 4)})

        scored.sort(key=lambda x: x["score"], reverse=True)

        # ── Detect regime ──
        spy_closes_full = [spy_prices[d] for d in all_dates[:rank_idx+1]
                           if d in spy_prices]
        regime = detect_regime(spy_closes_full)
        year   = year_from_date(rank_date)

        # ── Measure forward returns ──
        for horizon in HORIZONS:
            fwd_idx = rank_idx + horizon
            if fwd_idx >= len(all_dates):
                continue
            fwd_date = all_dates[fwd_idx]

            scores_list  = []
            returns_list = []
            spy_fwd_ret  = None

            # SPY forward return for benchmark
            if fwd_date in spy_prices and rank_date in spy_prices:
                p0 = spy_prices[rank_date]
                p1 = spy_prices[fwd_date]
                if p0 > 0:
                    spy_fwd_ret = (p1 - p0) / p0

            for s in scored:
                t = s["ticker"]
                prices = all_prices.get(t, {})
                p0 = prices.get(rank_date)
                p1 = prices.get(fwd_date)
                if p0 and p1 and p0 > 0:
                    fwd_ret = (p1 - p0) / p0
                    scores_list.append(s["score"])
                    returns_list.append(fwd_ret)

            ic = spearman_ic(scores_list, returns_list)

            # Top 5 vs bottom 5 spread
            n = len(scored)
            top_n    = max(1, n // 5)
            top_tickers  = [s["ticker"] for s in scored[:top_n]]
            bot_tickers  = [s["ticker"] for s in scored[-top_n:]]

            def avg_fwd(tickers):
                rets = []
                for t in tickers:
                    p0 = all_prices.get(t, {}).get(rank_date)
                    p1 = all_prices.get(t, {}).get(fwd_date)
                    if p0 and p1 and p0 > 0:
                        rets.append((p1 - p0) / p0)
                return statistics.mean(rets) if rets else None

            top_ret = avg_fwd(top_tickers)
            bot_ret = avg_fwd(bot_tickers)
            spread  = (top_ret - bot_ret) if (top_ret is not None and
                                               bot_ret is not None) else None

            # Top 5 beat SPY?
            top5_tickers = [s["ticker"] for s in scored[:5]]
            top5_beats   = 0
            top5_total   = 0
            for t in top5_tickers:
                p0 = all_prices.get(t, {}).get(rank_date)
                p1 = all_prices.get(t, {}).get(fwd_date)
                if p0 and p1 and p0 > 0 and spy_fwd_ret is not None:
                    stock_ret = (p1 - p0) / p0
                    top5_total += 1
                    if stock_ret > spy_fwd_ret:
                        top5_beats += 1
            hit_rate = (top5_beats / top5_total) if top5_total > 0 else None

            observations.append({
                "date":        rank_date,
                "year":        year,
                "regime":      regime,
                "horizon":     horizon,
                "ic":          ic,
                "spread":      round(spread, 4) if spread is not None else None,
                "top_ret":     round(top_ret, 4) if top_ret is not None else None,
                "bot_ret":     round(bot_ret, 4) if bot_ret is not None else None,
                "spy_ret":     round(spy_fwd_ret, 4) if spy_fwd_ret is not None else None,
                "hit_rate":    round(hit_rate, 3) if hit_rate is not None else None,
                "n_stocks":    len(scored),
                "top_5":       ",".join(top5_tickers),
            })

        # Progress
        print(f"  {rank_date} | Regime: {regime:<15} | "
              f"#1: {scored[0]['ticker']} | "
              f"Last: {scored[-1]['ticker']}")

    return observations


# ── Analysis & Reporting ──────────────────────────────────────────────────────
def safe_mean(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 4) if vals else None

def safe_std(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.stdev(vals), 4) if len(vals) >= 2 else None

def safe_pct_positive(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)

def stability_flag(mean_ic, pct_pos, std_ic, n):
    if n < 5 or mean_ic is None:
        return "⚪ INSUFFICIENT"
    if mean_ic >= 0.03 and pct_pos >= 60 and (std_ic or 1) < 0.08:
        return "🟢 STABLE EDGE"
    if mean_ic >= 0.01 and pct_pos >= 50:
        return "🟡 EMERGING"
    if mean_ic > 0:
        return "🟠 WEAK"
    if mean_ic < -0.02:
        return "🔴 CONTRARIAN"
    return "⚫ NO SIGNAL"


def print_full_report(observations: list):
    """Print comprehensive IC analysis report."""
    SEP = "═" * 72

    print(f"\n{SEP}")
    print(f"  MODEL A BACKTEST — FULL REPORT")
    print(f"  Pure Price Momentum | 2020–2026 | 30 Blue Chips")
    print(SEP)

    # ── Overall IC ──
    print(f"\n  {'─'*70}")
    print(f"  1. OVERALL IC SUMMARY")
    print(f"  {'─'*70}")
    print(f"  {'HORIZON':<10} {'MEAN IC':>8} {'MED IC':>8} {'STD IC':>8} "
          f"{'POS%':>6} {'N':>5}  STATUS")
    print(f"  {'─'*65}")

    for h in HORIZONS:
        ics = [o["ic"] for o in observations
               if o["horizon"] == h and o["ic"] is not None]
        mean_ic = safe_mean(ics)
        med_ic  = safe_mean(sorted(ics)[len(ics)//2:len(ics)//2+1]) if ics else None
        std_ic  = safe_std(ics)
        pct_pos = safe_pct_positive(ics)
        flag    = stability_flag(mean_ic, pct_pos or 0, std_ic, len(ics))

        print(f"  T+{h:<8} "
              f"{str(mean_ic) if mean_ic is not None else '—':>8}  "
              f"{str(round(statistics.median(ics),4)) if ics else '—':>7}  "
              f"{str(std_ic) if std_ic is not None else '—':>7}  "
              f"{str(pct_pos)+'%' if pct_pos is not None else '—':>6}  "
              f"{len(ics):>4}  {flag}")

    # ── IC by Year ──
    print(f"\n  {'─'*70}")
    print(f"  2. IC BY YEAR — Regime Survival Test")
    print(f"  {'─'*70}")
    print(f"  {'YEAR':<6} {'T+1 IC':>8} {'T+5 IC':>8} {'T+20 IC':>8} "
          f"{'T+1 POS%':>9} {'VERDICT'}")
    print(f"  {'─'*65}")

    years = sorted(set(o["year"] for o in observations))
    for year in years:
        row = f"  {year:<6}"
        verdicts = []
        for h in HORIZONS:
            ics = [o["ic"] for o in observations
                   if o["year"] == year and o["horizon"] == h
                   and o["ic"] is not None]
            mean_ic = safe_mean(ics)
            row += f"  {str(mean_ic) if mean_ic is not None else '—':>7}"
            if h == 1:
                pct = safe_pct_positive(ics)
                row += f"  {str(pct)+'%' if pct else '—':>8}"
                if mean_ic is not None:
                    verdicts.append(mean_ic)

        verdict = "✅ Positive" if verdicts and verdicts[0] > 0 else "❌ Negative"
        print(row + f"  {verdict}")

    # ── IC by Regime ──
    print(f"\n  {'─'*70}")
    print(f"  3. IC BY REGIME — Where Does Momentum Work?")
    print(f"  {'─'*70}")
    print(f"  {'REGIME':<18} {'T+1 IC':>8} {'T+5 IC':>8} {'T+20 IC':>8} "
          f"{'N':>5}  VERDICT")
    print(f"  {'─'*65}")

    regimes = sorted(set(o["regime"] for o in observations))
    for regime in regimes:
        row = f"  {regime:<18}"
        n_obs = len([o for o in observations
                     if o["regime"] == regime and o["horizon"] == 1])
        for h in HORIZONS:
            ics = [o["ic"] for o in observations
                   if o["regime"] == regime and o["horizon"] == h
                   and o["ic"] is not None]
            mean_ic = safe_mean(ics)
            row += f"  {str(mean_ic) if mean_ic is not None else '—':>7}"
        t1_ics = [o["ic"] for o in observations
                  if o["regime"] == regime and o["horizon"] == 1
                  and o["ic"] is not None]
        verdict = "✅" if safe_mean(t1_ics) and safe_mean(t1_ics) > 0 else "❌"
        print(row + f"  {n_obs:>4}  {verdict}")

    # ── Top/Bottom Spread ──
    print(f"\n  {'─'*70}")
    print(f"  4. TOP DECILE vs BOTTOM DECILE SPREAD")
    print(f"  {'─'*70}")
    print(f"  {'HORIZON':<10} {'TOP RET':>8} {'BOT RET':>8} "
          f"{'SPREAD':>8} {'SPY RET':>8}  ALPHA")
    print(f"  {'─'*65}")

    for h in HORIZONS:
        obs_h = [o for o in observations
                 if o["horizon"] == h and o["spread"] is not None]
        top  = safe_mean([o["top_ret"] for o in obs_h])
        bot  = safe_mean([o["bot_ret"] for o in obs_h])
        sprd = safe_mean([o["spread"]  for o in obs_h])
        spy  = safe_mean([o["spy_ret"] for o in obs_h
                          if o["spy_ret"] is not None])
        alpha = (round(top - spy, 4)
                 if top is not None and spy is not None else None)
        alpha_s = f"+{alpha:.4f}" if alpha and alpha > 0 else str(alpha)
        print(f"  T+{h:<8} {str(top):>8}  {str(bot):>8}  "
              f"{str(sprd):>8}  {str(spy):>8}  {alpha_s if alpha else '—'}")

    # ── Hit Rate ──
    print(f"\n  {'─'*70}")
    print(f"  5. HIT RATE — Top 5 Names Beating SPY")
    print(f"  {'─'*70}")
    print(f"  {'HORIZON':<10} {'HIT RATE':>10} {'TARGET':>8}  VERDICT")
    print(f"  {'─'*45}")

    for h in HORIZONS:
        hrs = [o["hit_rate"] for o in observations
               if o["horizon"] == h and o["hit_rate"] is not None]
        mean_hr = safe_mean(hrs)
        verdict = "✅ Above random" if mean_hr and mean_hr > 0.5 else "❌ Below random"
        print(f"  T+{h:<8} {str(mean_hr)+' ' if mean_hr else '—':>10}  "
              f"{'> 50%':>8}  {verdict}")

    # ── Key Findings ──
    print(f"\n{SEP}")
    print(f"  KEY FINDINGS & NEXT STEPS")
    print(SEP)

    t1_ics_all = [o["ic"] for o in observations
                  if o["horizon"] == 1 and o["ic"] is not None]
    overall_ic = safe_mean(t1_ics_all)
    pct_pos    = safe_pct_positive(t1_ics_all)

    if overall_ic is None:
        print("  Insufficient data for conclusions.")
    elif overall_ic >= 0.03 and pct_pos >= 60:
        print("  ✅ PROCEED: Momentum shows stable, positive IC.")
        print("     → Price momentum is a valid factor in this universe.")
        print("     → Add earnings revisions (Model B) and test incremental IC.")
        print("     → Begin Version 0 live tracking.")
    elif overall_ic > 0 and pct_pos >= 50:
        print("  🟡 CAUTIOUS: Momentum shows weak positive IC.")
        print("     → Signal exists but is fragile.")
        print("     → Test whether earnings revisions strengthen it (Model B).")
        print("     → Run live tracking in parallel. Do not deploy capital yet.")
    elif overall_ic > 0 and pct_pos < 50:
        print("  🟠 UNSTABLE: IC is positive on average but inconsistent.")
        print("     → Regime dependency likely. Review IC by regime table above.")
        print("     → Improve regime filter before live tracking.")
    elif overall_ic <= 0:
        print("  🔴 RETHINK: Momentum IC is negative or zero.")
        print("     → Pure momentum may be contrarian in this universe.")
        print("     → This does NOT mean Models B/C will fail.")
        print("     → Test earnings revisions independently.")
        print("     → Consider adjusting universe or lookback periods.")

    print(f"\n  {'─'*70}")
    print(f"  Remember: Model A failing ≠ whole system failing.")
    print(f"  It means price momentum alone is insufficient.")
    print(f"  Catalyst + quality factors may still produce IC.")
    print(f"  {'─'*70}\n")


def save_results(observations: list):
    """Save all observations to CSV."""
    if not observations:
        return
    out_file = RESULTS_DIR / "model_a_observations.csv"
    fieldnames = ["date", "year", "regime", "horizon", "ic",
                  "spread", "top_ret", "bot_ret", "spy_ret",
                  "hit_rate", "n_stocks", "top_5"]
    with open(out_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(observations)
    print(f"  Full results → {out_file}")

    # Summary CSV
    sum_file = RESULTS_DIR / "model_a_summary.csv"
    summary_rows = []
    for h in HORIZONS:
        for year in sorted(set(o["year"] for o in observations)):
            ics = [o["ic"] for o in observations
                   if o["horizon"] == h and o["year"] == year
                   and o["ic"] is not None]
            summary_rows.append({
                "horizon": h,
                "year":    year,
                "mean_ic": safe_mean(ics),
                "std_ic":  safe_std(ics),
                "pct_pos": safe_pct_positive(ics),
                "n":       len(ics),
            })
    with open(sum_file, "w", newline="") as f:
        writer = csv.DictWriter(f,
            fieldnames=["horizon","year","mean_ic","std_ic","pct_pos","n"])
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"  Summary      → {sum_file}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*72}")
    print(f"  MODEL A HISTORICAL BACKTESTER")
    print(f"  Version 0 Research Engine")
    print(f"  Start: {START_DATE}  |  Ranking every {RANKING_FREQ} trading days")
    print(f"{'='*72}")

    # Load universe
    with open(UNIVERSE, newline="") as f:
        stocks = list(csv.DictReader(f))
    print(f"\n  Universe: {len(stocks)} stocks")

    # Fetch all price histories
    print(f"\n  Fetching price history (cached after first run)...")
    all_prices = {}
    for s in stocks:
        print(f"    {s['ticker']}...", end=" ", flush=True)
        data = fetch_history(s["ticker"])
        if data:
            all_prices[s["ticker"]] = data["history"]
            print(f"OK ({len(data['history'])} days)")
        else:
            print("SKIP")

    # Fetch SPY
    print(f"    SPY...", end=" ", flush=True)
    spy_data = fetch_history("SPY")
    spy_prices = spy_data["history"] if spy_data else {}
    print(f"OK ({len(spy_prices)} days)" if spy_prices else "FAILED")

    # Fetch sector ETFs
    sector_prices = {}
    for sector, etf in SECTOR_ETFS.items():
        print(f"    {etf} ({sector})...", end=" ", flush=True)
        data = fetch_history(etf)
        if data:
            sector_prices[etf] = data["history"]
            print(f"OK ({len(data['history'])} days)")
        else:
            print("SKIP")

    print(f"\n  Running backtest...\n")
    observations = run_backtest(stocks, all_prices, spy_prices, sector_prices)

    if not observations:
        print("\n  [ERROR] No observations generated.")
        print("  Check network connection and try again.")
        sys.exit(1)

    print(f"\n  Total observations: {len(observations)}")
    print_full_report(observations)
    save_results(observations)


if __name__ == "__main__":
    main()
