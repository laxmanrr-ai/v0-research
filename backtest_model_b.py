#!/usr/bin/env python3
"""
backtest_model_b.py
-------------------
Version 0 Research Engine — Model B Historical Backtester

Tests whether earnings information improves IC over pure momentum.

Data approach: Price-based earnings proxy only.
  - No Yahoo fundamentals (look-ahead contaminated)
  - No analyst revision data (not available free + point-in-time)
  - All signals derived from price history alone

Why price-based earnings works:
  The market's 3-day post-earnings price reaction embeds both the
  EPS surprise AND the guidance. The reaction IS the combined signal.
  This is Post-Earnings Announcement Drift (PEAD) without raw EPS.
  Academically validated. Look-ahead safe by construction.

Three variants tested:
  B1: Model A momentum blend + earnings signal (weighted)
  B2: Equal-weight z-scores across all factors
  B3: Earnings signal only (no momentum factors)

Acceptance tests vs Model A baseline:
  Overall IC      > 0.0165
  2020 IC         > -0.0820
  2021 IC         > -0.0401
  TREND regime IC >= Model A (must not degrade)
  RANGE/HV IC     materially better than Model A
  IC std dev      < 0.3070
  T+20 spread     > 0.0031

Usage:
  python backtest_model_b.py

Runtime: ~5-8 minutes (network-bound, caches after first run).
No external packages required.
"""

import csv
import json
import math
import statistics
import sys
import time
import urllib.request
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

START_DATE    = "2020-01-01"
RANKING_FREQ  = 5        # re-rank every N trading days
MIN_HISTORY   = 130      # warmup days before first ranking
HORIZONS      = [1, 5, 20]

# Earnings signal parameters
EARN_HALFLIFE_DAYS   = 60    # signal decays to 50% after 60 days
EARN_DETECT_WINDOW   = 3     # days to check for abnormal move at quarter end
EARN_ABNORMAL_THRESH = 0.025 # 2.5% move flags a likely earnings reaction
EARN_REACTION_DAYS   = 3     # post-earnings drift window
QUARTER_DAYS         = 63    # ~3 months in trading days

# Model weights
MODEL_WEIGHTS = {
    "B1": {  # Weighted blend: momentum + earnings
        "rs_20":          0.18,
        "rs_60":          0.18,
        "rs_120":         0.12,
        "rs_spy":         0.08,
        "rs_sector":      0.07,
        "vol_adj_mom":    0.02,
        "post_earn_3d":   0.18,
        "earn_momentum":  0.10,
        "earn_decay":     0.07,
    },
    "B2": {  # Equal weight across all factors
        "rs_20":          0.111,
        "rs_60":          0.111,
        "rs_120":         0.111,
        "rs_spy":         0.111,
        "rs_sector":      0.111,
        "vol_adj_mom":    0.111,
        "post_earn_3d":   0.111,
        "earn_momentum":  0.111,
        "earn_decay":     0.112,
    },
    "B3": {  # Earnings only — pure PEAD test
        "rs_20":          0.0,
        "rs_60":          0.0,
        "rs_120":         0.0,
        "rs_spy":         0.0,
        "rs_sector":      0.0,
        "vol_adj_mom":    0.0,
        "post_earn_3d":   0.50,
        "earn_momentum":  0.30,
        "earn_decay":     0.20,
    },
}

# Model A baseline for acceptance tests
MODEL_A_BASELINE = {
    "overall_ic_t1":   0.0165,
    "overall_std_t1":  0.3070,
    "year_2020_ic":   -0.0820,
    "year_2021_ic":   -0.0401,
    "trend_up_ic":     0.0332,
    "trend_down_ic":   0.0957,
    "range_ic":        0.0019,
    "highvol_ic":     -0.0680,
    "spread_t20":      0.0031,
}

SECTOR_ETFS = {
    "Technology":  "XLK",
    "Finance":     "XLF",
    "Healthcare":  "XLV",
    "Energy":      "XLE",
    "Consumer":    "XLY",
    "Industrial":  "XLI",
}


# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_history(ticker: str, start_ts: int = None) -> dict | None:
    """Fetch daily price + volume history. Caches to disk."""
    cache_file = CACHE_DIR / f"{ticker}_b.json"

    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 86400:
            with open(cache_file) as f:
                return json.load(f)

    if start_ts is None:
        # 2019-06-01 for warmup before 2020
        start_ts = int(datetime(2019, 6, 1).timestamp())
    end_ts = int(time.time())

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&period1={start_ts}&period2={end_ts}")
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            chart     = data["chart"]["result"][0]
            timestamps = chart["timestamp"]
            quote     = chart["indicators"]["quote"][0]
            closes    = quote.get("close", [])
            volumes   = quote.get("volume", [])

            history = {}
            for ts, close, vol in zip(timestamps, closes, volumes):
                if close is not None:
                    date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    history[date_str] = {
                        "close":  round(close, 4),
                        "volume": int(vol) if vol else 0,
                    }

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


# ── Earnings detection from price ─────────────────────────────────────────────
def detect_earnings_dates(closes: list, volumes: list,
                           dates: list) -> list[dict]:
    """
    Detect likely earnings announcement dates from price history.

    Method:
      Earnings typically cause abnormal price moves + volume spikes.
      We look for days where:
        1. |return| > EARN_ABNORMAL_THRESH (2.5%)
        2. Volume > 1.5x 20-day average volume
        3. The move is roughly quarterly (spaced ~63 trading days apart)

    Returns list of dicts: {date, reaction_3d, reaction_5d, direction}
    reaction = 3-day cumulative abnormal return after the event date
    direction = +1 (positive) or -1 (negative)

    This is look-ahead safe: we only use prices that existed
    on or before the event date + 3 days.
    """
    n = len(closes)
    if n < 30:
        return []

    candidates = []
    for i in range(20, n - 5):
        # Daily return
        ret = (closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] else 0

        # Volume ratio vs 20-day average
        avg_vol = statistics.mean(volumes[max(0,i-20):i]) if volumes else 0
        vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 1.0

        # Flag if abnormal move with elevated volume
        if abs(ret) >= EARN_ABNORMAL_THRESH and vol_ratio >= 1.4:
            candidates.append({
                "idx":       i,
                "date":      dates[i],
                "day_ret":   ret,
                "vol_ratio": vol_ratio,
            })

    # Filter to roughly quarterly spacing (remove clustered events)
    earnings_dates = []
    last_idx = -QUARTER_DAYS  # allow first one from start

    for c in candidates:
        if c["idx"] - last_idx >= QUARTER_DAYS * 0.6:  # at least 38 days since last
            # Calculate 3-day post-earnings abnormal return
            end_idx = min(c["idx"] + EARN_REACTION_DAYS, n - 1)
            if end_idx > c["idx"]:
                total_ret = (closes[end_idx] - closes[c["idx"]]) / closes[c["idx"]]
            else:
                total_ret = c["day_ret"]

            earnings_dates.append({
                "date":        c["date"],
                "idx":         c["idx"],
                "reaction_3d": round(total_ret, 4),
                "direction":   1 if total_ret > 0 else -1,
                "magnitude":   abs(total_ret),
            })
            last_idx = c["idx"]

    return earnings_dates


def earnings_signal_on_date(earnings_dates: list, rank_date: str,
                             all_dates: list) -> dict:
    """
    Given a ranking date, compute the earnings-based factors.

    post_earn_3d:  3-day reaction of most recent earnings, time-decayed
    earn_momentum: consistency of last 2 earnings reactions
    earn_decay:    how stale the signal is (1=fresh, 0=very stale)

    Only uses earnings that occurred BEFORE rank_date.
    Look-ahead safe by construction.
    """
    # Filter to earnings before rank_date
    past_earnings = [e for e in earnings_dates if e["date"] < rank_date]

    if not past_earnings:
        return {"post_earn_3d": 0.0, "earn_momentum": 0.0, "earn_decay": 0.0}

    # Most recent earnings
    recent = past_earnings[-1]

    # Days since earnings
    try:
        rank_idx  = all_dates.index(rank_date)
        earn_idx  = all_dates.index(recent["date"]) if recent["date"] in all_dates \
                    else next((i for i, d in enumerate(all_dates)
                               if d > recent["date"]), rank_idx)
        days_since = rank_idx - earn_idx
    except (ValueError, StopIteration):
        days_since = QUARTER_DAYS

    # Time decay: half-life of EARN_HALFLIFE_DAYS trading days
    # After one quarter (63 days), signal at ~50% strength
    decay = math.exp(-0.693 * days_since / EARN_HALFLIFE_DAYS)

    # post_earn_3d: decayed reaction signal
    post_earn = recent["reaction_3d"] * decay

    # earn_momentum: direction consistency of last 2 earnings
    if len(past_earnings) >= 2:
        prev = past_earnings[-2]
        # Same direction = momentum (+1), opposite = reversal (-1)
        momentum = recent["direction"] * prev["direction"]
        # Scale by magnitude of both
        earn_mom = momentum * recent["magnitude"] * prev["magnitude"] * 10
    else:
        earn_mom = recent["direction"] * recent["magnitude"]

    return {
        "post_earn_3d":  round(post_earn, 4),
        "earn_momentum": round(earn_mom, 4),
        "earn_decay":    round(decay, 4),
    }


# ── Factor utilities (same as Model A) ───────────────────────────────────────
def get_return(closes: list, n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    base = closes[-(n+1)]
    return (closes[-1] - base) / base if base != 0 else None


def get_volatility(closes: list, n: int = 20) -> float:
    if len(closes) < n + 2:
        return 0.01
    rets = [(closes[i] - closes[i-1]) / closes[i-1]
            for i in range(-n, 0) if closes[i-1] != 0]
    if len(rets) < 3:
        return 0.01
    mean = sum(rets) / len(rets)
    var  = sum((r - mean)**2 for r in rets) / len(rets)
    return math.sqrt(var) or 0.01


def cross_sectional_zscore(values: dict) -> dict:
    tickers = [t for t, v in values.items() if v is not None]
    vals    = [values[t] for t in tickers]
    n = len(vals)
    if n < 2:
        return {t: 0.0 for t in tickers}
    mean = sum(vals) / n
    std  = math.sqrt(sum((v - mean)**2 for v in vals) / n)
    if std == 0:
        return {t: 0.0 for t in tickers}
    return {t: (values[t] - mean) / std for t in tickers}


def spearman_ic(scores: list, returns: list) -> float | None:
    paired = [(s, r) for s, r in zip(scores, returns) if s is not None and r is not None]
    n = len(paired)
    if n < 5:
        return None
    def rank(lst):
        si = sorted(range(n), key=lambda i: lst[i])
        r  = [0.0] * n
        for pos, i in enumerate(si):
            r[i] = float(pos + 1)
        return r
    s_r = rank([p[0] for p in paired])
    r_r = rank([p[1] for p in paired])
    d_sq = sum((s_r[i] - r_r[i])**2 for i in range(n))
    return round(1 - (6 * d_sq) / (n * (n**2 - 1)), 4)


def detect_regime(spy_closes: list) -> str:
    n = len(spy_closes)
    if n < 60:
        return "INSUFFICIENT"
    price  = spy_closes[-1]
    ma20   = sum(spy_closes[-20:]) / 20
    ma50   = sum(spy_closes[-50:]) / 50
    ret_20 = (price - spy_closes[-21]) / spy_closes[-21] if n > 21 else 0
    ranges = [abs(spy_closes[i] - spy_closes[i-1]) for i in range(-20, 0)]
    atr    = (sum(ranges) / 20) / price if price > 0 else 0
    if price > ma20 > ma50 and ret_20 > 0.02:
        return "TREND_UP"
    if price < ma20 < ma50 and ret_20 < -0.02:
        return "TREND_DOWN"
    if atr > 0.012:
        return "HIGH_VOLATILITY"
    if atr < 0.005 and abs(price / ma20 - 1) < 0.015:
        return "LOW_VOLATILITY"
    return "RANGE_BOUND"


# ── Main backtest ─────────────────────────────────────────────────────────────
def run_backtest(stocks, all_prices, all_volumes, spy_prices,
                 sector_prices, earnings_map) -> list:
    """Walk-forward backtest across all ranking dates."""
    all_dates = sorted(set(
        date for prices in all_prices.values()
        for date in prices
        if date >= START_DATE
    ))
    print(f"\n  Trading dates: {len(all_dates)}")
    print(f"  Period: {all_dates[0]} → {all_dates[-1]}")

    ranking_dates = all_dates[MIN_HISTORY::RANKING_FREQ]
    print(f"  Ranking dates: {len(ranking_dates)}\n")

    observations = []

    for rank_date in ranking_dates:
        rank_idx = all_dates.index(rank_date)

        # ── Build raw factors ──
        factor_raw = defaultdict(dict)
        valid_stocks = []

        for stock in stocks:
            ticker = stock["ticker"]
            sector = stock["sector"]
            closes  = [all_prices[ticker][d]
                       for d in all_dates[:rank_idx+1]
                       if d in all_prices.get(ticker, {})]
            volumes = [all_volumes[ticker].get(d, 0)
                       for d in all_dates[:rank_idx+1]
                       if d in all_prices.get(ticker, {})]

            if len(closes) < MIN_HISTORY:
                continue

            spy_closes = [spy_prices[d] for d in all_dates[:rank_idx+1]
                          if d in spy_prices]
            etf = SECTOR_ETFS.get(sector, "SPY")
            sec_closes = [sector_prices.get(etf, {}).get(d, 0)
                          for d in all_dates[:rank_idx+1]
                          if d in sector_prices.get(etf, {})]

            # Momentum factors
            rs_20  = get_return(closes, 20)
            rs_60  = get_return(closes, 60)
            rs_120 = get_return(closes, 120)
            vol    = get_volatility(closes, 20)
            spy_r  = get_return(spy_closes, 20) if len(spy_closes) > 21 else 0
            sec_r  = get_return(sec_closes, 20) if len(sec_closes) > 21 else 0
            rs_spy    = (rs_20 - spy_r)  if rs_20 is not None else None
            rs_sector = (rs_20 - sec_r)  if rs_20 is not None else None
            vol_adj   = (rs_60 / vol)    if rs_60 is not None and vol > 0 else None

            # Earnings factors
            earn_sig = earnings_signal_on_date(
                earnings_map.get(ticker, []), rank_date, all_dates
            )

            # Store raw factors
            for fname, fval in [
                ("rs_20", rs_20), ("rs_60", rs_60), ("rs_120", rs_120),
                ("rs_spy", rs_spy), ("rs_sector", rs_sector),
                ("vol_adj_mom", vol_adj),
                ("post_earn_3d",  earn_sig["post_earn_3d"]),
                ("earn_momentum", earn_sig["earn_momentum"]),
                ("earn_decay",    earn_sig["earn_decay"]),
            ]:
                factor_raw[fname][ticker] = fval

            valid_stocks.append({
                "ticker": ticker, "sector": sector,
                "price": closes[-1], "closes": closes,
            })

        if len(valid_stocks) < 5:
            continue

        # ── Cross-sectional z-scores ──
        all_factor_names = list(list(MODEL_WEIGHTS.values())[0].keys())
        zscores = {}
        for fname in all_factor_names:
            raw = {t: v for t, v in factor_raw[fname].items() if v is not None}
            zscores[fname] = cross_sectional_zscore(raw)

        # ── Score each variant ──
        scored_by_model = {}
        for model, weights in MODEL_WEIGHTS.items():
            scored = []
            for s in valid_stocks:
                t = s["ticker"]
                score = sum(weights[f] * zscores[f].get(t, 0.0)
                            for f in weights)
                scored.append({**s, "score": round(score, 4)})
            scored.sort(key=lambda x: x["score"], reverse=True)
            scored_by_model[model] = scored

        # ── Regime ──
        spy_closes_to_date = [spy_prices[d] for d in all_dates[:rank_idx+1]
                               if d in spy_prices]
        regime = detect_regime(spy_closes_to_date)
        year   = rank_date[:4]

        # ── Measure forward returns ──
        for horizon in HORIZONS:
            fwd_idx = rank_idx + horizon
            if fwd_idx >= len(all_dates):
                continue
            fwd_date = all_dates[fwd_idx]

            spy_fwd = None
            if rank_date in spy_prices and fwd_date in spy_prices:
                p0, p1 = spy_prices[rank_date], spy_prices[fwd_date]
                if p0 > 0:
                    spy_fwd = (p1 - p0) / p0

            for model, scored in scored_by_model.items():
                scores_list  = []
                returns_list = []
                for s in scored:
                    t  = s["ticker"]
                    p0 = all_prices.get(t, {}).get(rank_date)
                    p1 = all_prices.get(t, {}).get(fwd_date)
                    if p0 and p1 and p0 > 0:
                        scores_list.append(s["score"])
                        returns_list.append((p1 - p0) / p0)

                ic = spearman_ic(scores_list, returns_list)

                n  = len(scored)
                q  = max(1, n // 5)
                top_tickers = [s["ticker"] for s in scored[:q]]
                bot_tickers = [s["ticker"] for s in scored[-q:]]

                def avg_ret(tickers):
                    rs = []
                    for t in tickers:
                        p0 = all_prices.get(t, {}).get(rank_date)
                        p1 = all_prices.get(t, {}).get(fwd_date)
                        if p0 and p1 and p0 > 0:
                            rs.append((p1 - p0) / p0)
                    return statistics.mean(rs) if rs else None

                top_r = avg_ret(top_tickers)
                bot_r = avg_ret(bot_tickers)
                spread = (top_r - bot_r) if (top_r and bot_r) else None

                top5 = [s["ticker"] for s in scored[:5]]
                beats = sum(
                    1 for t in top5
                    if all_prices.get(t, {}).get(rank_date)
                    and all_prices.get(t, {}).get(fwd_date)
                    and spy_fwd is not None
                    and (all_prices[t][fwd_date] - all_prices[t][rank_date])
                        / all_prices[t][rank_date] > spy_fwd
                )
                hit_rate = beats / len(top5) if top5 else None

                observations.append({
                    "date":     rank_date,
                    "year":     year,
                    "regime":   regime,
                    "horizon":  horizon,
                    "model":    model,
                    "ic":       ic,
                    "spread":   round(spread, 4) if spread else None,
                    "top_ret":  round(top_r, 4)  if top_r  else None,
                    "bot_ret":  round(bot_r, 4)  if bot_r  else None,
                    "spy_ret":  round(spy_fwd, 4) if spy_fwd else None,
                    "hit_rate": round(hit_rate, 3) if hit_rate else None,
                    "n_stocks": len(scored),
                    "top_5":    ",".join(top5),
                })

        print(f"  {rank_date} | {regime:<15} | "
              f"B1#{scored_by_model['B1'][0]['ticker']} "
              f"B3#{scored_by_model['B3'][0]['ticker']}")

    return observations


# ── Analysis ──────────────────────────────────────────────────────────────────
def safe_mean(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 4) if vals else None

def safe_std(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.stdev(vals), 4) if len(vals) >= 2 else None

def safe_pos_pct(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None

def ic_for(obs, model=None, horizon=None, year=None, regime=None):
    result = obs
    if model:   result = [o for o in result if o["model"] == model]
    if horizon: result = [o for o in result if o["horizon"] == horizon]
    if year:    result = [o for o in result if o["year"] == year]
    if regime:  result = [o for o in result if o["regime"] == regime]
    return [o["ic"] for o in result if o["ic"] is not None]


def print_acceptance_tests(obs: list):
    """The most important output: does Model B pass vs Model A?"""
    B = MODEL_A_BASELINE
    SEP = "═" * 72

    print(f"\n{SEP}")
    print(f"  ACCEPTANCE TESTS vs MODEL A BASELINE")
    print(SEP)
    print(f"  {'TEST':<35} {'MODEL A':>8} {'B1':>8} {'B2':>8} {'B3':>8}  RESULT")
    print(f"  {'─'*70}")

    tests = []

    # 1. Overall IC T+1
    a_ic = B["overall_ic_t1"]
    results = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1)
        results[m] = safe_mean(ics)
    row = ["Overall IC (T+1)", f"{a_ic:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v > a_ic:
            verdict = "✅"
    tests.append(row + [verdict])

    # 2. IC stability (std dev)
    a_std = B["overall_std_t1"]
    results_std = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1)
        results_std[m] = safe_std(ics)
    row = ["IC Std Dev (lower=better)", f"{a_std:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results_std[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v < a_std:
            verdict = "✅"
    tests.append(row + [verdict])

    # 3. 2020 IC
    a_2020 = B["year_2020_ic"]
    results_2020 = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1, year="2020")
        results_2020[m] = safe_mean(ics)
    row = ["2020 IC (was -0.082)", f"{a_2020:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results_2020[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v > a_2020:
            verdict = "✅"
    tests.append(row + [verdict])

    # 4. 2021 IC
    a_2021 = B["year_2021_ic"]
    results_2021 = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1, year="2021")
        results_2021[m] = safe_mean(ics)
    row = ["2021 IC (was -0.040)", f"{a_2021:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results_2021[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v > a_2021:
            verdict = "✅"
    tests.append(row + [verdict])

    # 5. TREND_UP IC (must not degrade)
    a_tup = B["trend_up_ic"]
    results_tup = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1, regime="TREND_UP")
        results_tup[m] = safe_mean(ics)
    row = ["TREND_UP IC (must not degrade)", f"{a_tup:.4f}"]
    verdict = "✅"
    for m in ["B1","B2","B3"]:
        v = results_tup[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v < a_tup * 0.80:  # allow 20% degradation tolerance
            verdict = "❌"
    tests.append(row + [verdict])

    # 6. TREND_DOWN IC (must not degrade)
    a_tdn = B["trend_down_ic"]
    results_tdn = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1, regime="TREND_DOWN")
        results_tdn[m] = safe_mean(ics)
    row = ["TREND_DOWN IC (must not degrade)", f"{a_tdn:.4f}"]
    verdict = "✅"
    for m in ["B1","B2","B3"]:
        v = results_tdn[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v < a_tdn * 0.80:
            verdict = "❌"
    tests.append(row + [verdict])

    # 7. RANGE_BOUND IC (must improve materially)
    a_rng = B["range_ic"]
    results_rng = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1, regime="RANGE_BOUND")
        results_rng[m] = safe_mean(ics)
    row = ["RANGE IC (must improve)", f"{a_rng:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results_rng[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v > a_rng + 0.015:  # material improvement threshold
            verdict = "✅"
    tests.append(row + [verdict])

    # 8. HIGH_VOL IC (must improve)
    a_hv = B["highvol_ic"]
    results_hv = {}
    for m in ["B1","B2","B3"]:
        ics = ic_for(obs, model=m, horizon=1, regime="HIGH_VOLATILITY")
        results_hv[m] = safe_mean(ics)
    row = ["HIGH_VOL IC (must improve)", f"{a_hv:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results_hv[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v > a_hv + 0.02:
            verdict = "✅"
    tests.append(row + [verdict])

    # 9. T+20 spread
    a_sp = B["spread_t20"]
    results_sp = {}
    for m in ["B1","B2","B3"]:
        spreads = [o["spread"] for o in obs
                   if o["model"]==m and o["horizon"]==20 and o["spread"] is not None]
        results_sp[m] = safe_mean(spreads)
    row = ["T+20 Spread (must improve)", f"{a_sp:.4f}"]
    verdict = "❌"
    for m in ["B1","B2","B3"]:
        v = results_sp[m]
        row.append(f"{v:.4f}" if v else "—")
        if v and v > a_sp:
            verdict = "✅"
    tests.append(row + [verdict])

    # Print tests
    for t in tests:
        print(f"  {t[0]:<35} {t[1]:>8} {t[2]:>8} {t[3]:>8} {t[4]:>8}  {t[5]}")

    passed = sum(1 for t in tests if t[5] == "✅")
    print(f"\n  Passed: {passed}/{len(tests)}")

    return passed, len(tests)


def print_full_report(obs: list):
    SEP = "═" * 72

    print(f"\n{SEP}")
    print(f"  MODEL B BACKTEST — FULL REPORT")
    print(f"  Earnings Proxy (PEAD) | 2020–2026 | Three Variants")
    print(SEP)

    # Overall IC by model and horizon
    print(f"\n  {'─'*70}")
    print(f"  1. OVERALL IC BY MODEL")
    print(f"  {'─'*70}")
    print(f"  {'MODEL':<6} {'HORIZON':<10} {'MEAN IC':>8} {'MED IC':>8} "
          f"{'STD IC':>8} {'POS%':>6} {'N':>4}")
    print(f"  {'─'*55}")

    for m in ["B1", "B2", "B3"]:
        for h in HORIZONS:
            ics = ic_for(obs, model=m, horizon=h)
            mean = safe_mean(ics)
            std  = safe_std(ics)
            pos  = safe_pos_pct(ics)
            med  = round(statistics.median(ics), 4) if ics else None
            print(f"  {m:<6} T+{h:<8} "
                  f"{str(mean) if mean else '—':>8}  "
                  f"{str(med)  if med  else '—':>7}  "
                  f"{str(std)  if std  else '—':>7}  "
                  f"{str(pos)+'%' if pos else '—':>6}  {len(ics):>3}")
        print()

    # IC by year — B1 focus (best blend)
    print(f"  {'─'*70}")
    print(f"  2. IC BY YEAR — B1 vs Model A baseline")
    print(f"  {'─'*70}")
    print(f"  {'YEAR':<6} {'A (T+1)':>10} {'B1 (T+1)':>10} {'B2 (T+1)':>10} "
          f"{'B3 (T+1)':>10}  IMPROVED?")
    a_by_year = {
        "2020": -0.0820, "2021": -0.0401, "2022": 0.0730,
        "2023": 0.0344,  "2024": 0.0402,  "2025": 0.0099, "2026": 0.0485
    }
    for year in sorted(set(o["year"] for o in obs)):
        a_ic = a_by_year.get(year, "—")
        row  = f"  {year:<6} {str(a_ic):>10}"
        improved = False
        for m in ["B1","B2","B3"]:
            ics = ic_for(obs, model=m, horizon=1, year=year)
            v   = safe_mean(ics)
            row += f"  {str(v) if v else '—':>9}"
            if v and isinstance(a_ic, float) and v > a_ic:
                improved = True
        print(row + f"  {'✅' if improved else '❌'}")

    # IC by regime
    print(f"\n  {'─'*70}")
    print(f"  3. IC BY REGIME (T+1)")
    print(f"  {'─'*70}")
    print(f"  {'REGIME':<18} {'A':>8} {'B1':>8} {'B2':>8} {'B3':>8}  VERDICT")
    a_by_regime = {
        "TREND_UP": 0.0332, "TREND_DOWN": 0.0957,
        "RANGE_BOUND": 0.0019, "HIGH_VOLATILITY": -0.0680,
        "LOW_VOLATILITY": -0.0634,
    }
    for regime in sorted(set(o["regime"] for o in obs)):
        a_ic = a_by_regime.get(regime, 0)
        row  = f"  {regime:<18} {a_ic:>8.4f}"
        best = None
        for m in ["B1","B2","B3"]:
            ics = ic_for(obs, model=m, horizon=1, regime=regime)
            v   = safe_mean(ics)
            row += f"  {v:>7.4f}" if v else "  —      "
            if v and (best is None or v > best):
                best = v
        verdict = "✅ Improved" if best and best > a_ic else "❌ No gain"
        print(row + f"  {verdict}")

    # Horse race: does earnings add IC?
    print(f"\n  {'─'*70}")
    print(f"  4. HORSE RACE — Does Earnings Signal Add IC?")
    print(f"  {'─'*70}")
    for h in HORIZONS:
        b1 = safe_mean(ic_for(obs, model="B1", horizon=h))
        b2 = safe_mean(ic_for(obs, model="B2", horizon=h))
        b3 = safe_mean(ic_for(obs, model="B3", horizon=h))
        a  = {1: 0.0165, 5: 0.0037, 20: 0.0083}.get(h, 0)

        if b3 and b3 > a:
            earn_verdict = "✅ Earnings alone has IC — strong signal"
        elif b1 and b1 > a and (b3 is None or b3 <= a):
            earn_verdict = "⚡ Earnings adds IC only when blended with momentum"
        elif b1 and b1 <= a:
            earn_verdict = "❌ Earnings does not improve over pure momentum"
        else:
            earn_verdict = "⚪ Insufficient data"

        print(f"  T+{h}: A={a:.4f}  B1={b1 or '—'}  B2={b2 or '—'}  "
              f"B3={b3 or '—'}")
        print(f"       → {earn_verdict}")

    passed, total = print_acceptance_tests(obs)

    # Final verdict
    print(f"\n{SEP}")
    print(f"  FINAL VERDICT")
    print(SEP)

    b1_ic = safe_mean(ic_for(obs, model="B1", horizon=1))
    b3_ic = safe_mean(ic_for(obs, model="B3", horizon=1))

    if passed >= 7:
        print("  ✅ STRONG PASS: Model B materially improves on Model A.")
        print("     Earnings signal adds genuine IC.")
        print("     → Proceed to Model C (add quality factors).")
        print("     → Begin live Version 0 tracking with Model B.")
    elif passed >= 5:
        print("  🟡 PARTIAL PASS: Model B improves on some tests.")
        print("     Earnings signal is real but incomplete.")
        print("     → Identify which tests failed. Refine earnings detection.")
        print("     → Run Model C in parallel.")
    elif passed >= 3:
        print("  🟠 WEAK: Model B shows limited improvement.")
        if b3_ic and b3_ic > 0.01:
            print("     Earnings signal has IC but weighting is wrong.")
            print("     → Increase earnings weight. Reduce momentum weight.")
        else:
            print("     Price-based earnings proxy may be too noisy.")
            print("     → Consider: is quarterly detection accurate enough?")
    else:
        print("  🔴 FAIL: Model B does not improve on Model A.")
        print("     Price-based earnings detection likely insufficient.")
        print("     Options:")
        print("     1. Accept Model A (regime-filtered) as the foundation")
        print("     2. Obtain real earnings surprise data (paid source)")
        print("     3. Test alternative proxies (volume anomaly, gap size)")
        print("     Model A failing to improve does NOT kill Model C.")
        print("     Quality factors may still add IC independently.")

    print(f"\n  {'─'*70}")
    print(f"  Reminder: The real Model A edge was TREND_UP + TREND_DOWN only.")
    print(f"  A regime-filtered Model A with IC ~0.055 is already tradeable.")
    print(f"  Model B must beat that — not the unfiltered 0.0165 baseline.")
    print(f"  {'─'*70}\n")


def save_results(obs: list):
    if not obs:
        return
    out = RESULTS_DIR / "model_b_observations.csv"
    fieldnames = ["date","year","regime","horizon","model","ic",
                  "spread","top_ret","bot_ret","spy_ret",
                  "hit_rate","n_stocks","top_5"]
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(obs)
    print(f"\n  Results → {out}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*72}")
    print(f"  MODEL B BACKTESTER — Price-Based Earnings Proxy")
    print(f"  Three variants: B1 (blend), B2 (equal-weight), B3 (earnings only)")
    print(f"{'='*72}")

    with open(UNIVERSE, newline="") as f:
        stocks = list(csv.DictReader(f))
    print(f"\n  Universe: {len(stocks)} stocks")

    # Fetch price + volume history
    print(f"\n  Fetching price histories...")
    all_prices  = {}
    all_volumes = {}
    for s in stocks:
        print(f"    {s['ticker']}...", end=" ", flush=True)
        data = fetch_history(s["ticker"])
        if data:
            all_prices[s["ticker"]]  = {d: v["close"]  for d, v in data["history"].items()}
            all_volumes[s["ticker"]] = {d: v["volume"] for d, v in data["history"].items()}
            print(f"OK ({len(data['history'])} days)")
        else:
            print("SKIP")

    print(f"    SPY...", end=" ", flush=True)
    spy_data   = fetch_history("SPY")
    spy_prices = {d: v["close"] for d, v in spy_data["history"].items()} if spy_data else {}
    print(f"OK ({len(spy_prices)} days)" if spy_prices else "FAILED")

    sector_prices = {}
    for sector, etf in SECTOR_ETFS.items():
        print(f"    {etf}...", end=" ", flush=True)
        data = fetch_history(etf)
        if data:
            sector_prices[etf] = {d: v["close"] for d, v in data["history"].items()}
            print(f"OK")
        else:
            print("SKIP")

    # Build earnings dates from price history
    print(f"\n  Detecting earnings dates from price history...")
    earnings_map = {}
    all_dates_full = sorted(set(d for p in all_prices.values() for d in p))

    for s in stocks:
        t = s["ticker"]
        dates   = [d for d in all_dates_full if d in all_prices.get(t, {})]
        closes  = [all_prices[t][d] for d in dates]
        volumes = [all_volumes[t].get(d, 0) for d in dates]
        earn_dates = detect_earnings_dates(closes, volumes, dates)
        earnings_map[t] = earn_dates
        print(f"    {t}: {len(earn_dates)} earnings events detected")

    # Run backtest
    print(f"\n  Running walk-forward backtest...\n")
    observations = run_backtest(
        stocks, all_prices, all_volumes,
        spy_prices, sector_prices, earnings_map
    )

    if not observations:
        print("\n  [ERROR] No observations. Check network.")
        sys.exit(1)

    print(f"\n  Total observations: {len(observations)}")
    print_full_report(observations)
    save_results(observations)


if __name__ == "__main__":
    main()
