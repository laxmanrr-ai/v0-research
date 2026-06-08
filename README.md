# Version 0 Research Engine
## B-Adaptive Live Tracking System

---

## What This Is

A measurement instrument.

Its job is to answer one question over 180 days:

> Does the B-adaptive ranking system have predictive power
> in live conditions?

It is not a trading system.
It does not tell you what to buy.
Capital deployment decisions come after the 180-day window closes.

---

## File Structure

```
v0_research/
├── universe.csv          ← 30 blue chips (FROZEN)
├── daily_ranker.py       ← Run after every market close
├── ic_tracker.py         ← Run every Friday after close
├── backtest_model_a.py   ← Historical reference (do not modify)
├── backtest_model_b.py   ← Historical reference (do not modify)
├── backtest_model_c.py   ← Historical reference (do not modify)
├── research_notes.md     ← Log ideas here. Touch nothing else.
├── live_rankings.csv     ← Auto-generated, grows daily
├── prices.csv            ← Auto-generated, grows daily
├── regime_log.csv        ← Auto-generated, grows daily
├── ic_log.csv            ← Auto-generated weekly
└── README.md             ← This file
```

---

## Daily Run Sequence

**After every market close (Mon–Fri):**
```bash
python daily_ranker.py
```

**After every Friday close:**
```bash
python ic_tracker.py
```

That is the full discipline. Nothing else.

---

## Active Model: B-Adaptive

| Regime | Model | Confidence |
|---|---|---|
| TREND_UP | B2 | Normal |
| TREND_DOWN | B2 | Normal |
| RANGE_BOUND | B3 | Normal |
| HIGH_VOLATILITY | B3 | Caution — paper only |
| LOW_VOLATILITY | B3 | Caution — paper only |

**B2:** Equal weight across 9 factors (momentum + earnings blend)
**B3:** Earnings dominant (post_earn 50%, earn_momentum 30%, earn_decay 20%)

---

## Checkpoints

| Day | Decision | Question |
|---|---|---|
| 20 | None | First T+1 IC readings. Directional only. |
| 45 | None | First T+5 readings. Still fragile. |
| 90 | Continuation | Did the hypothesis survive first contact? |
| 180 | Evidence | Is there predictive power worth acting on? |

**Day 90 is not a capital deployment decision.**
It is a continuation decision: keep measuring, or stop and investigate.

**Day 180 is not a full deployment decision.**
If evidence is positive: small real-money experiment (5–10% of capital)
for learning about execution costs — not return generation.

---

## Backtest Baselines (for comparison at checkpoints)

| Metric | Value |
|---|---|
| T+1 IC | 0.0165 (std: 0.307) |
| T+5 IC | 0.0037 (std: 0.289) |
| T+20 IC | 0.0196 (std: 0.263) |
| T+20 spread | 0.0054 |
| TREND_UP IC | 0.0441 |
| TREND_DOWN IC | 0.1100 |
| RANGE_BOUND IC | 0.0111 |

Signal-to-noise ratio at T+20: **0.074**
This is a weak signal. It requires honest measurement to confirm.
After costs, net T+20 spread ≈ 0.34% — thin margin of safety.

---

## Frozen Methodology

Do not change any of the following during the 180-day window:

- Universe composition
- B2 / B3 factor weights
- Regime classification rules
- Earnings signal parameters
- IC calculation method
- Rank persistence metrics
- Checkpoint criteria

Log all ideas in `research_notes.md`.
Act on none of them until Day 180.

**If you change the methodology mid-test, you cannot know whether
improvement came from signal quality or from moving the goalposts.**

---

## The Asset You Are Building

The ranked dataset that accumulates over 180 days.

Not the ranked list itself.
Not the IC numbers themselves.
The longitudinal evidence that either confirms or rejects
the hypothesis that this ranking system has predictive power.

That dataset — and the discipline used to build it —
is worth more than any individual signal.

---

## How This Project Evolved

| Stage | Question |
|---|---|
| Start | How do I make 1% daily? |
| Review 1–2 | How do I build a repeatable edge? |
| Review 3–4 | How do I discover what conditions produce edge? |
| Review 5–6 | Can I reproduce known factors before inventing new ones? |
| Backtest A | Does momentum have IC in this universe? (Weakly yes) |
| Backtest B | Does earnings improve IC? (Yes — especially 2020/2021) |
| Backtest C | Does quality improve B? (No — hurts T+20) |
| Now | Does live IC match backtest structure over 180 days? |

---

*Version 0 — Measurement phase*
*Capital at risk: $0*
*Next milestone: Day 90 continuation decision*
