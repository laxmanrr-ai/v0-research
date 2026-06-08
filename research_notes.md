# Research Notes
## Version 0 Live Tracking — Ideas Log

**Purpose of this file:**
Log observations, hypotheses, and improvement ideas during the 180-day
measurement window WITHOUT touching the running system.

The live test only means something if the methodology is frozen.
Any idea recorded here becomes a candidate for Version 1 —
after the 180-day window closes with a clear verdict.

---

## Frozen Methodology (do not change during live test)

| Component | Frozen Value |
|---|---|
| Universe | 30 blue chips in universe.csv |
| TREND_UP / TREND_DOWN | B2 weights (equal 0.111 across 9 factors) |
| RANGE / HIGH_VOL / LOW_VOL | B3 weights (post_earn 0.50, earn_mom 0.30, earn_decay 0.20) |
| Earnings detection | 2.5% threshold, 1.4x volume, 60-day half-life |
| Quality veto | Bottom-quartile Yahoo fundamentals composite |
| IC method | Spearman rank correlation |
| Rank persistence | Avg rank change, top-5/10 turnover, persistence score |
| Checkpoints | Day 90 continuation, Day 180 evidence |

---

## 180-Day Window

Start date: _(fill in when first daily_ranker.py is run)_
Day 90 checkpoint: _(fill in)_
Day 180 checkpoint: _(fill in)_

---

## Pre-Start Corrections (made before clock started — not protocol violations)

### [June 2026] — Signal naming correction
What the system calls an "earnings signal" is not strictly PEAD
(Post-Earnings Announcement Drift). It is a Significant Quarterly
Reaction Signal (SQRS): the price and volume response to the most
significant information event of each quarter — which may be earnings,
a product launch, a regulatory decision, or a macro print.

The backtest IC already reflects this noise since it ran on real
historical data containing all event types. The measurement is
internally consistent. But the label overstates what we know.

Correct interpretation going forward:
  "We are measuring whether the market's strongest quarterly reactions
   have drift — regardless of what caused those reactions."

No code change required. Language correction only.

---

## Ideas Log — Version 1 Candidates

_These are R&D assets. None are operational emergencies.
Act on none of them until Day 180._

---

### [June 2026] — Regime boundary sensitivity (threshold quantization error)

Source: External review + self-identified.

The ±2% 20-day return threshold for TREND classification is arbitrary.
A market printing +1.9% behaves almost identically to one printing +2.1%,
yet the system draws a hard line flipping between B2 and B3.

Near the boundary, the system will whipsaw — switching models day-to-day
when SPY consolidates around the 2% mark. This generates conflicting
signals and, in live trading, unnecessary transaction costs.

Version 1 test: compare IC under +1.0%, +1.5%, +2.0%, +2.5% thresholds.

---

### [June 2026] — 3-regime vs 5-regime system

Source: External review + self-identified.

Current system has 5 regimes: TREND_UP, TREND_DOWN, RANGE_BOUND,
HIGH_VOLATILITY, LOW_VOLATILITY.

A 3-regime system (TREND, RANGE, HIGH_VOL) would:
  - Reduce boundary noise from splitting TREND_UP/TREND_DOWN
  - Reduce false regime switches at the ±2% boundary
  - Simplify the model switch logic

Version 1 test: backtest 3-regime vs 5-regime on same universe.
Hypothesis: 3-regime reduces IC variance without reducing mean IC.

---

### [June 2026] — Regime detection lag

Source: External review.

All regime indicators (MA20, MA50, 20-day return, ATR) are lagging.
By the time TREND_DOWN is confirmed, the sharpest move may be over.
By the time HIGH_VOL is detected, the spike may be reverting.

This is structural — no lagging-indicator regime system avoids it.
But the lag could be measured: how many days after a regime shift
does classification actually change? If lag is >5 days on average,
the regime switch may be adding noise rather than signal.

Version 1 test: measure regime classification lag against ex-post
regime labels. Quantify IC degradation attributable to lag.

---

### [June 2026] — Conditional overlay / T-bill deployment

Source: External review — capital efficiency reframe.

When regime = RANGE_BOUND, HIGH_VOL, or LOW_VOL (~50% of trading days),
the current system parks capital as "paper only."

A full portfolio architecture would deploy that capital into:
  - Short-term T-bills (risk-free yield, currently ~5%)
  - A completely uncorrelated strategy
  - Cash, if no better option

This changes the Sharpe/Sortino profile materially. The strategy
is not "always-on capital drag" — it is a conditional overlay
where non-deployed capital earns risk-free yield.

Version 1 consideration: model the total portfolio return including
T-bill yield during non-deployment periods. Compare to buy-and-hold.

---

### [June 2026] — HIGH_VOLATILITY circuit breaker validation

Source: Regime architecture review.

The current waterfall prioritizes HIGH_VOLATILITY detection before
TREND detection. This means a crashing market (TREND_DOWN + HIGH_VOL)
gets classified as HIGH_VOLATILITY and uses B3 with a caution flag —
not B2 momentum.

This is intentional and defensible: in a crash, volatility explodes
before trend confirmation, so HIGH_VOL fires first as a circuit breaker.

Version 1 validation: check whether HIGH_VOL classification during
actual crash periods (March 2020, Q4 2022) correctly suppressed
the TREND_DOWN signal. Measure IC for dates that would have been
TREND_DOWN but were reclassified as HIGH_VOL.

---

### [June 2026] — Quarterly event type tagging

Source: Signal naming correction above.

If a paid earnings data source becomes available (e.g. Polygon.io at
$29/mo, WRDS), tag detected quarterly events as:
  - Confirmed earnings date
  - Non-earnings event (macro, product, regulatory)

Then measure IC separately for each event type.
Hypothesis: confirmed earnings events have higher SQRS IC than
non-earnings events. If true, earnings filtering improves B3 IC.
If false, the market's reaction to any major event has equal drift —
and the signal needs no earnings data at all.

---

_End of pre-start research notes. All further entries during the
180-day window should be dated and appended below this line._

