#!/usr/bin/env python3
"""
ic_tracker.py  —  Live IC Measurement Engine
=============================================
Version 0 Research Engine

PURPOSE
-------
This script measures whether the B-adaptive ranking system
has any predictive power in live conditions.

It does NOT tell you whether to trade.
It tells you where you are in the measurement process.

CHECKPOINTS
-----------
Day 20:   First IC readings available (T+1 only). Directional only.
Day 45:   First T+5 readings. Still fragile. Do not conclude.
Day 90:   CONTINUATION DECISION — not capital deployment decision.
          ~15-25 independent T+20 observations. Ask:
            PASS:      IC positive, directionally consistent with backtest
                       → Continue measurement
            FAIL:      IC materially negative, ranking inversion
                       → Stop and investigate
            AMBIGUOUS: IC near zero, high variance (most likely outcome)
                       → Continue gathering data
Day 180:  EVIDENCE CHECKPOINT — first real question: is there
          predictive power? Compare live vs backtest structure.
          Not exact match. Qualitative survival.

CAPITAL DECISION
----------------
Not at day 90. Not at day 180.
Only when:
  1. Live IC positive and stable across 2+ regime shifts
  2. Rank persistence confirms signal stability
  3. Small real-money experiment (5-10% capital) is the NEXT step
     — for learning about execution costs and psychology —
     not for return generation.

METRICS TRACKED
---------------
  IC (T+1, T+5, T+20)          — Does ranking predict returns?
  IC by regime                 — Does structure match backtest?
  IC rolling windows (5,10,20) — Is signal stable or decaying?
  Rank persistence             — Is this signal or noise?
  Top-5 turnover               — How stable are top candidates?
  Spread (top vs bottom decile)— Is there economic value?
  Backtest comparison table    — Qualitative survival check

BACKTEST BASELINES (B-adaptive)
--------------------------------
  T+1  mean IC:  0.0165  std: 0.307
  T+5  mean IC:  0.0037  std: 0.289
  T+20 mean IC:  0.0196  std: 0.263
  T+20 spread:   0.0054
  TREND_UP IC:   0.0441
  TREND_DOWN IC: 0.1100
  RANGE IC:      0.0111
  HIGH_VOL IC:  -0.0680

HONEST CONTEXT
--------------
  T+20 IC of 0.0196 with std 0.263 = signal-to-noise ratio of 0.074
  After costs, net spread ~0.34% per 20 days = ~8.5% annualized
  90 days ≈ 15-25 independent T+20 observations
  This is a hypothesis under test, not a proven edge.

USAGE
-----
  python ic_tracker.py          # weekly, ideally Friday after close
"""

import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LIVE_RANKINGS = BASE_DIR / "data" / "live_rankings.csv"
PRICES        = BASE_DIR / "data" / "prices.csv"
IC_LOG        = BASE_DIR / "data" / "ic_log.csv"

HORIZONS = [1, 5, 20]

# Backtest baselines for comparison
BACKTEST = {
    "ic_t1":          0.0165,
    "ic_t5":          0.0037,
    "ic_t20":         0.0196,
    "std_t1":         0.3070,
    "std_t5":         0.2890,
    "std_t20":        0.2634,
    "spread_t20":     0.0054,
    "pos_pct_t1":     51.2,
    "TREND_UP":       0.0441,
    "TREND_DOWN":     0.1100,
    "RANGE_BOUND":    0.0111,
    "HIGH_VOLATILITY":-0.0680,
    "LOW_VOLATILITY": -0.0634,
}


# ── Utilities ─────────────────────────────────────────────────────────────────
def load_csv(path: Path) -> list:
    if not path.exists(): return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def sm(v):
    v = [x for x in v if x is not None]
    return round(statistics.mean(v), 4) if v else None

def ss(v):
    v = [x for x in v if x is not None]
    return round(statistics.stdev(v), 4) if len(v) >= 2 else None

def sp(v):
    v = [x for x in v if x is not None]
    return round(sum(1 for x in v if x > 0) / len(v) * 100, 1) if v else None

def spearman_ic(scores: list, returns: list) -> float | None:
    paired = [(s, r) for s, r in zip(scores, returns)
              if s is not None and r is not None]
    n = len(paired)
    if n < 5: return None
    def rank(lst):
        si = sorted(range(n), key=lambda i: lst[i])
        r  = [0.0] * n
        for pos, i in enumerate(si): r[i] = float(pos + 1)
        return r
    sr = rank([p[0] for p in paired])
    rr = rank([p[1] for p in paired])
    d  = sum((sr[i]-rr[i])**2 for i in range(n))
    return round(1 - (6*d)/(n*(n**2-1)), 4)

def trading_days_elapsed(start_date_str: str) -> int:
    """Approximate trading days since start date."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    today = date.today()
    if today <= start: return 0
    # Rough: 5/7 of calendar days are trading days
    cal_days = (today - start).days
    return int(cal_days * 5 / 7)


# ── Forward return calculation ─────────────────────────────────────────────────
def fill_forward_returns(rankings: list, prices: list) -> list:
    """
    For each ranking row, find the actual T+1/T+5/T+20 forward return
    using the prices file. Updates ret_t1/t5/t20 fields.
    """
    # Build price index: {ticker: {date: price}}
    price_idx = defaultdict(dict)
    for p in prices:
        price_idx[p["ticker"]][p["date"]] = float(p["price"])

    # Sorted list of all dates in prices
    all_dates = sorted(set(p["date"] for p in prices))

    updated = []
    for r in rankings:
        rank_date = r["date"]
        ticker    = r["ticker"]
        row       = dict(r)

        try:
            base_idx = all_dates.index(rank_date)
        except ValueError:
            updated.append(row)
            continue

        p0 = price_idx[ticker].get(rank_date)
        if not p0:
            updated.append(row)
            continue

        for h, field in [(1,"ret_t1"),(5,"ret_t5"),(20,"ret_t20")]:
            if row.get(field): continue  # already filled
            fwd_idx = base_idx + h
            if fwd_idx < len(all_dates):
                fwd_date = all_dates[fwd_idx]
                p1 = price_idx[ticker].get(fwd_date)
                if p1 and p0 > 0:
                    row[field] = round((p1-p0)/p0, 6)
        updated.append(row)
    return updated


# ── IC calculation ─────────────────────────────────────────────────────────────
def calc_ic_by_date(rankings: list) -> dict:
    """
    For each ranking date and horizon, compute IC across all stocks.
    Returns {date: {horizon: ic_value}}
    """
    by_date = defaultdict(list)
    for r in rankings:
        by_date[r["date"]].append(r)

    results = {}
    for rank_date, rows in sorted(by_date.items()):
        results[rank_date] = {}
        regime = rows[0].get("regime","UNKNOWN")
        results[rank_date]["regime"] = regime

        for h, field in [(1,"ret_t1"),(5,"ret_t5"),(20,"ret_t20")]:
            scores  = []
            returns = []
            for r in rows:
                try:
                    score = float(r["score"])
                    ret   = float(r[field]) if r.get(field) else None
                    if ret is not None:
                        scores.append(score)
                        returns.append(ret)
                except (ValueError, TypeError):
                    continue
            results[rank_date][h] = spearman_ic(scores, returns)

    return results


# ── Rank persistence ───────────────────────────────────────────────────────────
def calc_rank_persistence(rankings: list) -> dict:
    """
    Measures how stable rankings are day-to-day.

    Metrics:
      avg_rank_change:  average absolute rank change between consecutive days
      top5_turnover:    fraction of top-5 names that change each day
      top10_turnover:   fraction of top-10 names that change each day
      persistence_score: 1 - avg_rank_change/N  (higher = more stable)

    High turnover + weak IC = noise.
    Stable rankings + modest IC = potentially useful signal.
    """
    by_date = defaultdict(dict)
    for r in rankings:
        by_date[r["date"]][r["ticker"]] = int(r["rank"])

    dates = sorted(by_date.keys())
    if len(dates) < 2:
        return {"n_days": len(dates), "insufficient": True}

    rank_changes    = []
    top5_turnovers  = []
    top10_turnovers = []

    for i in range(1, len(dates)):
        prev = by_date[dates[i-1]]
        curr = by_date[dates[i]]
        common = set(prev.keys()) & set(curr.keys())
        if not common: continue

        # Avg absolute rank change
        changes = [abs(curr[t] - prev[t]) for t in common]
        rank_changes.extend(changes)

        # Top-5 turnover
        prev_top5 = {t for t,r in prev.items() if r <= 5}
        curr_top5 = {t for t,r in curr.items() if r <= 5}
        if prev_top5:
            top5_turnovers.append(
                len(prev_top5 - curr_top5) / len(prev_top5))

        # Top-10 turnover
        prev_top10 = {t for t,r in prev.items() if r <= 10}
        curr_top10 = {t for t,r in curr.items() if r <= 10}
        if prev_top10:
            top10_turnovers.append(
                len(prev_top10 - curr_top10) / len(prev_top10))

    n = len(set(by_date[dates[0]].keys()))  # universe size
    avg_change = sm(rank_changes)
    persistence = round(1 - avg_change/n, 4) if avg_change and n > 0 else None

    return {
        "n_days":           len(dates),
        "avg_rank_change":  avg_change,
        "top5_turnover":    sm(top5_turnovers),
        "top10_turnover":   sm(top10_turnovers),
        "persistence_score": persistence,
        "interpretation":   _persistence_interpretation(
            persistence, sm(top5_turnovers))
    }

def _persistence_interpretation(persistence, top5_turnover) -> str:
    if persistence is None: return "Insufficient data"
    if persistence >= 0.85 and top5_turnover and top5_turnover < 0.3:
        return "Stable — rankings change slowly. Potentially useful signal."
    if persistence >= 0.75 and top5_turnover and top5_turnover < 0.5:
        return "Moderate — reasonable stability."
    if persistence < 0.65 or (top5_turnover and top5_turnover > 0.6):
        return "Unstable — high turnover. Possible noise."
    return "Mixed — monitor further."


# ── Spread calculation ─────────────────────────────────────────────────────────
def calc_spread(rankings: list, horizon_field: str) -> dict:
    """Top quintile vs bottom quintile average return."""
    by_date = defaultdict(list)
    for r in rankings:
        ret = r.get(horizon_field)
        if ret:
            try:
                by_date[r["date"]].append(
                    (int(r["rank"]), float(ret)))
            except (ValueError, TypeError):
                pass

    spreads = []
    for date_rows in by_date.values():
        n = len(date_rows)
        if n < 5: continue
        date_rows.sort(key=lambda x: x[0])
        q = max(1, n//5)
        top_ret = statistics.mean(r for _, r in date_rows[:q])
        bot_ret = statistics.mean(r for _, r in date_rows[-q:])
        spreads.append(top_ret - bot_ret)

    return {
        "mean_spread": sm(spreads),
        "pos_pct":     sp(spreads),
        "n":           len(spreads),
    }


# ── Checkpoint evaluation ──────────────────────────────────────────────────────
def evaluate_checkpoint(ic_by_date: dict, n_trading_days: int,
                         rank_persistence: dict) -> tuple[str, str]:
    """
    Returns (checkpoint_label, verdict) based on days elapsed
    and current IC evidence.
    """
    t20_ics = [ic_by_date[d][20] for d in ic_by_date
               if ic_by_date[d].get(20) is not None]

    if n_trading_days < 20:
        return "PRE-DATA", "Accumulating data. No IC available yet."

    if n_trading_days < 45:
        t1_ics = [ic_by_date[d][1] for d in ic_by_date
                  if ic_by_date[d].get(1) is not None]
        mean = sm(t1_ics)
        return ("EARLY (T+1 only)",
                f"T+1 IC = {mean}. Directional only. Too early to conclude.")

    if n_trading_days < 90:
        t5_ics = [ic_by_date[d][5] for d in ic_by_date
                  if ic_by_date[d].get(5) is not None]
        mean = sm(t5_ics)
        return ("MID (T+1/T+5)",
                f"T+5 IC = {mean}. Building evidence. Do not conclude.")

    if n_trading_days < 180:
        # Day 90 checkpoint: continuation decision
        mean_t20 = sm(t20_ics)
        std_t20  = ss(t20_ics)
        pos_t20  = sp(t20_ics)

        if mean_t20 is None:
            return "DAY-90 CHECKPOINT", "Insufficient T+20 observations yet."

        if mean_t20 > 0.01 and pos_t20 and pos_t20 >= 50:
            verdict = (f"PASS — Continue measurement.\n"
                       f"     Live T+20 IC = {mean_t20} (backtest: 0.0196).\n"
                       f"     IC positive, hypothesis survived first contact.\n"
                       f"     Do NOT deploy capital. Continue to day 180.")
        elif mean_t20 < -0.02:
            verdict = (f"FAIL — Stop and investigate.\n"
                       f"     Live T+20 IC = {mean_t20}. Materially negative.\n"
                       f"     Ranking inversion possible. Review regime behavior.\n"
                       f"     Do not continue without understanding why.")
        else:
            verdict = (f"AMBIGUOUS — Continue gathering data.\n"
                       f"     Live T+20 IC = {mean_t20}. Near zero / high variance.\n"
                       f"     This is the most likely outcome at 90 days.\n"
                       f"     15-25 independent observations is not enough to conclude.\n"
                       f"     Continue to day 180.")

        return "DAY-90 CHECKPOINT (Continuation decision)", verdict

    # Day 180+: evidence checkpoint
    mean_t20 = sm(t20_ics)
    std_t20  = ss(t20_ics)
    pos_t20  = sp(t20_ics)

    if mean_t20 and mean_t20 > 0.015 and pos_t20 and pos_t20 >= 52:
        stable = rank_persistence.get("persistence_score", 0) or 0
        if stable >= 0.75:
            verdict = (f"EVIDENCE OF PREDICTIVE POWER.\n"
                       f"     Live IC = {mean_t20}, pos% = {pos_t20}%, "
                       f"persistence = {stable}.\n"
                       f"     Qualitative structure matches backtest.\n"
                       f"     Consider small real-money experiment:\n"
                       f"       → 5-10% of capital only\n"
                       f"       → Tracked separately\n"
                       f"       → Purpose: learn about execution costs\n"
                       f"         and psychology — NOT return generation.")
        else:
            verdict = (f"IC positive but rankings unstable.\n"
                       f"     Live IC = {mean_t20} but persistence = {stable}.\n"
                       f"     High turnover with modest IC often means noise.\n"
                       f"     Continue measurement. Do not deploy.")
    elif mean_t20 and mean_t20 > 0:
        verdict = (f"WEAK POSITIVE — Insufficient for deployment.\n"
                   f"     Live IC = {mean_t20}. Positive but fragile.\n"
                   f"     Continue measurement for another 90 days.\n"
                   f"     Investigate regime-specific performance.")
    else:
        verdict = (f"NO EVIDENCE — Do not deploy.\n"
                   f"     Live IC = {mean_t20}. Not positive at 180 days.\n"
                   f"     The edge may not exist in live conditions.\n"
                   f"     Review: data pipeline, universe, factor definitions.")

    return "DAY-180 EVIDENCE CHECKPOINT", verdict


# ── Main report ───────────────────────────────────────────────────────────────
def print_report(rankings: list, ic_by_date: dict,
                 rank_persistence: dict, first_date: str):
    SEP  = "═" * 70
    today_str = date.today().strftime("%Y-%m-%d")
    n_days = trading_days_elapsed(first_date)

    print(f"\n{SEP}")
    print(f"  IC TRACKER  —  Live Measurement Report")
    print(f"  Run date: {today_str}  |  Tracking since: {first_date}")
    print(f"  Trading days elapsed (approx): {n_days}")
    print(SEP)

    # ── Overall IC ──
    print(f"\n  {'─'*68}")
    print(f"  1. IC SUMMARY  (backtest baseline in parentheses)")
    print(f"  {'─'*68}")
    print(f"  {'HORIZON':<10} {'LIVE IC':>9} {'LIVE STD':>9} "
          f"{'POS%':>7} {'N':>5}  {'BACKTEST':>9}  DELTA")
    print(f"  {'─'*60}")

    for h, bt_ic, bt_std in [
        (1,  BACKTEST["ic_t1"],  BACKTEST["std_t1"]),
        (5,  BACKTEST["ic_t5"],  BACKTEST["std_t5"]),
        (20, BACKTEST["ic_t20"], BACKTEST["std_t20"]),
    ]:
        ics = [ic_by_date[d][h] for d in ic_by_date
               if ic_by_date[d].get(h) is not None]
        mean = sm(ics); std = ss(ics); pos = sp(ics)
        delta = round(mean - bt_ic, 4) if mean is not None else None
        delta_s = (f"+{delta}" if delta and delta >= 0 else str(delta)) if delta else "—"
        flag = "✅" if mean and mean > 0 else ("❌" if mean and mean < -0.01 else "⚪")
        print(f"  T+{h:<8} {str(mean):>9} {str(std):>9} "
              f"{str(pos)+'%' if pos else '—':>7} {len(ics):>5}  "
              f"{bt_ic:>9}  {delta_s}  {flag}")

    # ── IC by regime ──
    print(f"\n  {'─'*68}")
    print(f"  2. IC BY REGIME (T+1)  — Does structure match backtest?")
    print(f"  {'─'*68}")
    print(f"  {'REGIME':<20} {'LIVE IC':>9} {'N':>5}  {'BACKTEST':>9}  MATCH?")
    print(f"  {'─'*55}")

    regime_ics = defaultdict(list)
    for d, data in ic_by_date.items():
        regime = data.get("regime", "UNKNOWN")
        if data.get(1) is not None:
            regime_ics[regime].append(data[1])

    for regime in ["TREND_UP","TREND_DOWN","RANGE_BOUND",
                   "HIGH_VOLATILITY","LOW_VOLATILITY"]:
        ics   = regime_ics.get(regime, [])
        mean  = sm(ics)
        bt_ic = BACKTEST.get(regime, 0)
        if mean is None:
            print(f"  {regime:<20} {'—':>9} {'—':>5}  {bt_ic:>9}  ⚪ no data")
            continue
        # Structure match: same sign AND within 2x magnitude
        same_sign  = (mean >= 0) == (bt_ic >= 0)
        reasonable = abs(mean) <= abs(bt_ic) * 3
        match = "✅ yes" if same_sign else "❌ inverted"
        if same_sign and not reasonable:
            match = "⚠️  much stronger"
        print(f"  {regime:<20} {mean:>9.4f} {len(ics):>5}  "
              f"{bt_ic:>9.4f}  {match}")

    # ── Rolling IC stability ──
    print(f"\n  {'─'*68}")
    print(f"  3. ROLLING IC STABILITY (T+1)  — Signal or noise?")
    print(f"  {'─'*68}")

    t1_series = [(d, ic_by_date[d][1]) for d in sorted(ic_by_date.keys())
                 if ic_by_date[d].get(1) is not None]

    for window_label, n_obs in [("Rolling 5","5"),
                                  ("Rolling 10","10"),
                                  ("Rolling 20","20"),
                                  ("All-time","all")]:
        if n_obs == "all":
            window = [v for _, v in t1_series]
        else:
            window = [v for _, v in t1_series[-int(n_obs):]]
        if not window:
            continue
        mean = sm(window); std = ss(window); pos = sp(window)
        flag = "✅" if mean and mean > 0.01 else ("❌" if mean and mean < -0.01 else "⚪")
        print(f"  {window_label:<12} mean={str(mean):>8}  "
              f"std={str(std):>8}  pos%={str(pos)+'%' if pos else '—':>7}  "
              f"n={len(window):>3}  {flag}")

    # ── Spread ──
    print(f"\n  {'─'*68}")
    print(f"  4. TOP vs BOTTOM SPREAD  (backtest T+20: {BACKTEST['spread_t20']})")
    print(f"  {'─'*68}")
    print(f"  {'HORIZON':<12} {'SPREAD':>9} {'POS%':>7} {'N':>5}  vs backtest")
    for h, field, bt_sp in [
        (1,  "ret_t1",  0.0005),
        (5,  "ret_t5",  0.0007),
        (20, "ret_t20", 0.0054),
    ]:
        sp_data = calc_spread(rankings, field)
        mean    = sp_data["mean_spread"]
        pos     = sp_data["pos_pct"]
        n       = sp_data["n"]
        delta   = round(mean - bt_sp, 4) if mean else None
        flag    = "✅" if mean and mean > bt_sp else ("❌" if mean and mean < 0 else "⚪")
        print(f"  T+{h:<9}  {str(mean):>9} {str(pos)+'%' if pos else '—':>7} "
              f"{n:>5}  {flag} (bt: {bt_sp})")

    # ── Rank persistence ──
    print(f"\n  {'─'*68}")
    print(f"  5. RANK PERSISTENCE  — Signal stability diagnostic")
    print(f"  {'─'*68}")
    if rank_persistence.get("insufficient"):
        print(f"  Insufficient data ({rank_persistence['n_days']} days). Need 5+ days.")
    else:
        p = rank_persistence
        print(f"  Avg rank change per day:  {p.get('avg_rank_change','—')}")
        print(f"  Top-5 daily turnover:     "
              f"{str(round(p['top5_turnover']*100,1))+'%' if p.get('top5_turnover') else '—'}")
        print(f"  Top-10 daily turnover:    "
              f"{str(round(p['top10_turnover']*100,1))+'%' if p.get('top10_turnover') else '—'}")
        print(f"  Persistence score:        {p.get('persistence_score','—')}")
        print(f"  Assessment:               {p.get('interpretation','—')}")
        print()
        print(f"  Note: High turnover + weak IC = noise masquerading as signal.")
        print(f"        Stable rankings + modest IC = potentially useful signal.")

    # ── Backtest comparison ──
    print(f"\n  {'─'*68}")
    print(f"  6. BACKTEST vs LIVE COMPARISON")
    print(f"  {'─'*68}")
    t20_ics = [ic_by_date[d][20] for d in ic_by_date
               if ic_by_date[d].get(20) is not None]
    live_t20 = sm(t20_ics)
    live_std = ss(t20_ics)
    live_pos = sp(t20_ics)
    sp20     = calc_spread(rankings, "ret_t20")

    rows = [
        ("Mean IC (T+20)",    f"{BACKTEST['ic_t20']:.4f}",
         f"{live_t20:.4f}" if live_t20 else "—"),
        ("Std Dev (T+20)",    f"{BACKTEST['std_t20']:.4f}",
         f"{live_std:.4f}" if live_std else "—"),
        ("Positive IC %",     f"{BACKTEST['pos_pct_t1']:.1f}%",
         f"{live_pos:.1f}%" if live_pos else "—"),
        ("T+20 Spread",       f"{BACKTEST['spread_t20']:.4f}",
         f"{sp20['mean_spread']:.4f}" if sp20['mean_spread'] else "—"),
        ("TREND_UP IC",       f"{BACKTEST['TREND_UP']:.4f}",
         f"{sm(regime_ics.get('TREND_UP',[])):.4f}"
         if regime_ics.get("TREND_UP") else "—"),
        ("TREND_DOWN IC",     f"{BACKTEST['TREND_DOWN']:.4f}",
         f"{sm(regime_ics.get('TREND_DOWN',[])):.4f}"
         if regime_ics.get("TREND_DOWN") else "—"),
        ("RANGE_BOUND IC",    f"{BACKTEST['RANGE_BOUND']:.4f}",
         f"{sm(regime_ics.get('RANGE_BOUND',[])):.4f}"
         if regime_ics.get("RANGE_BOUND") else "—"),
    ]

    print(f"  {'METRIC':<24} {'BACKTEST':>10} {'LIVE':>10}  KEY QUESTION")
    print(f"  {'─'*65}")
    questions = [
        "Signal strength maintained?",
        "Variance reduced in live?",
        "Positive more than half?",
        "Economic value after costs?",
        "Trend regime behavior?",
        "Bear regime behavior?",
        "Range regime behavior?",
    ]
    for (label, bt, live), q in zip(rows, questions):
        print(f"  {label:<24} {bt:>10} {live:>10}  {q}")

    print(f"\n  The key is NOT exact match.")
    print(f"  The key is whether the qualitative structure survives:")
    print(f"    - Trend regimes better than range regimes")
    print(f"    - Rankings broadly monotonic")
    print(f"    - No systematic inversion")

    # ── Checkpoint verdict ──
    checkpoint, verdict = evaluate_checkpoint(
        ic_by_date, n_days, rank_persistence)

    print(f"\n{SEP}")
    print(f"  CHECKPOINT: {checkpoint}")
    print(f"  {'─'*68}")
    for line in verdict.split("\n"):
        print(f"  {line}")
    print(SEP)

    print(f"\n  {'─'*68}")
    print(f"  CAPITAL DEPLOYMENT FRAMEWORK")
    print(f"  {'─'*68}")
    print(f"  Day  90: Continuation decision only. Not deployment.")
    print(f"  Day 180: First evidence checkpoint.")
    print(f"  Post-180 (if IC confirmed):")
    print(f"    → Small real-money experiment: 5-10% of capital")
    print(f"    → Purpose: learn execution costs, not generate returns")
    print(f"    → Full deployment only after execution costs validated")
    print(f"  {'─'*68}\n")


def save_ic_log(ic_by_date: dict):
    """Append today's IC summary to ic_log.csv."""
    today = date.today().strftime("%Y-%m-%d")
    fields = ["log_date","rank_date","regime","ic_t1","ic_t5","ic_t20"]
    file_exists = IC_LOG.exists()
    with open(IC_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        for rank_date, data in sorted(ic_by_date.items()):
            writer.writerow({
                "log_date":  today,
                "rank_date": rank_date,
                "regime":    data.get("regime",""),
                "ic_t1":     data.get(1,""),
                "ic_t5":     data.get(5,""),
                "ic_t20":    data.get(20,""),
            })


def update_rankings_with_returns(rankings: list):
    """Write updated ret_t1/t5/t20 back to live_rankings.csv."""
    if not rankings: return
    fields = list(rankings[0].keys())
    with open(LIVE_RANKINGS, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rankings)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    rankings_raw = load_csv(LIVE_RANKINGS)
    prices       = load_csv(PRICES)

    if not rankings_raw:
        print("\n  No rankings found. Run daily_ranker.py first.")
        return

    n_days_tracked = len(set(r["date"] for r in rankings_raw))
    first_date     = sorted(set(r["date"] for r in rankings_raw))[0]
    print(f"\n  Loaded {len(rankings_raw)} ranking records.")
    print(f"  Tracking since: {first_date}  ({n_days_tracked} trading days logged)")

    if n_days_tracked < 2:
        print("  Need at least 2 days of data.")
        return

    # Fill forward returns where available
    rankings = fill_forward_returns(rankings_raw, prices)
    update_rankings_with_returns(rankings)

    # Calculate IC
    ic_by_date = calc_ic_by_date(rankings)

    # Rank persistence
    rank_persistence = calc_rank_persistence(rankings)

    # Print full report
    print_report(rankings, ic_by_date, rank_persistence, first_date)

    # Save IC log
    save_ic_log(ic_by_date)
    print(f"  IC log saved → {IC_LOG}\n")


if __name__ == "__main__":
    main()
