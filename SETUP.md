# Setup Guide
## Running V0 Research Engine on GitHub Actions

**Time required:** 20–30 minutes
**Cost:** Free
**Dependency:** GitHub account (free tier is sufficient)

---

## What This Does

Once set up, GitHub's servers will automatically:
- Run `daily_ranker.py` every weekday at 4:30pm ET
- Run `ic_tracker.py` every Friday at 5:00pm ET
- Commit the updated CSV files back to your repository
- Store 180 days of IC reports as downloadable artifacts

Your local machine does not need to be on.

---

## Step 1 — Create a GitHub Repository

1. Go to [github.com](https://github.com) and sign in
2. Click **New repository** (top right, + icon)
3. Name it: `v0-research` (or any name you prefer)
4. Set to **Private** (your data stays private)
5. Do NOT initialize with README (you have one already)
6. Click **Create repository**

GitHub will show you an empty repo page. Keep it open.

---

## Step 2 — Upload Your Files

You have two options:

### Option A — GitHub Desktop (easiest, no command line)

1. Download [GitHub Desktop](https://desktop.github.com)
2. File → Clone Repository → paste your repo URL
3. Copy all your v0_research files into the cloned folder:
   ```
   universe.csv
   daily_ranker.py
   ic_tracker.py
   backtest_model_a.py
   backtest_model_b.py
   backtest_model_c.py
   research_notes.md
   README.md
   .github/
   └── workflows/
       ├── daily_ranker.yml
       └── weekly_ic.yml
   ```
4. GitHub Desktop will show all files as "new"
5. Write commit message: "Initial setup"
6. Click **Commit to main** → **Push origin**

### Option B — Command Line

```bash
cd /path/to/your/v0_research_folder

git init
git remote add origin https://github.com/YOUR_USERNAME/v0-research.git
git add .
git commit -m "Initial setup"
git branch -M main
git push -u origin main
```

---

## Step 3 — Verify Workflows Are Detected

1. Go to your repository on GitHub
2. Click the **Actions** tab
3. You should see two workflows listed:
   - `Daily Ranker`
   - `Weekly IC Tracker`

If you don't see them, check that the files are in exactly:
`.github/workflows/daily_ranker.yml`
`.github/workflows/weekly_ic.yml`

---

## Step 4 — Run a Test Manually

Before waiting for the scheduled run:

1. Actions tab → **Daily Ranker** → **Run workflow** → **Run workflow**
2. Watch the run execute (takes 2–4 minutes)
3. Click into the run to see the output
4. When finished, check your repository — a `data/` folder should appear
   containing `live_rankings.csv`, `prices.csv`, `regime_log.csv`

If the run fails:
- Click the failed step to see the error
- Most likely cause: Yahoo Finance temporarily blocked the IP
- Solution: wait 10 minutes and run again manually

---

## Step 5 — Verify the Schedule

GitHub Actions scheduled jobs run on UTC time.

| Job | UTC | ET (EST) | ET (EDT/summer) |
|---|---|---|---|
| Daily Ranker | 21:30 | 4:30pm | 5:30pm |
| Weekly IC | 22:00 Fri | 5:00pm Fri | 6:00pm Fri |

**Important:** GitHub's free tier schedulers can be delayed by up to
30–60 minutes during busy periods. This is normal and does not affect
the data quality — the ranker uses end-of-day prices, not real-time.

---

## Step 6 — Monitor Going Forward

**Weekly check (2 minutes):**
1. GitHub → Actions tab
2. Verify last 5 runs show green checkmarks
3. Click Friday's IC run → download artifact → open ic_log.csv

**If a run fails (red X):**
1. Click the failed run
2. Read the error message
3. Most failures are temporary Yahoo Finance rate limits
4. Manual re-run usually fixes it: Actions → failed workflow → Re-run jobs

---

## Viewing Your Data

Your data lives at:
```
github.com/YOUR_USERNAME/v0-research/tree/main/data/
```

Files grow automatically:
- `live_rankings.csv` — one row per stock per day (30 stocks × ~250 days = ~7,500 rows at end)
- `prices.csv` — one row per stock per day
- `regime_log.csv` — one row per day
- `ic_log.csv` — one row per ranking date per week (updated each Friday)

You can download any file directly from GitHub's UI at any time.

---

## Important: The Freeze

The `.github/workflows/` files run exactly what is in your repository.

**Do not edit `daily_ranker.py` or `ic_tracker.py` during the 180-day window.**

If you change the code and push it, the next run uses the new code.
GitHub's commit history will show exactly what code ran on each date —
which is actually useful forensic evidence if you ever need to investigate
a result.

All ideas go in `research_notes.md`. That file is safe to edit.

---

## GitHub Actions Free Tier Limits

| Limit | Your usage | Status |
|---|---|---|
| 2,000 minutes/month | ~50 minutes/month | ✅ Well within |
| 500 MB storage | ~5 MB of CSVs | ✅ Well within |
| Concurrent jobs | 1 | ✅ Fine |

You will not hit any limits.

---

## Checkpoint Dates

Fill these in after your first successful run:

- **First run date:** _______________
- **Day 90 checkpoint:** _______________ (add 90 trading days)
- **Day 180 checkpoint:** _______________ (add 180 trading days)

A trading day calculator:
```
90 trading days  ≈ 18 calendar weeks ≈ 4.5 months
180 trading days ≈ 36 calendar weeks ≈ 9 months
```

---

## Questions

If a workflow fails repeatedly, the most likely causes are:

1. **Yahoo Finance rate limiting** — GitHub's IP ranges are sometimes
   blocked temporarily. Wait 1 hour and retry manually.

2. **Market holiday** — the script will run but find no new data.
   The duplicate guard will prevent double entries. This is fine.

3. **Git push permission error** — ensure the repository's Actions
   settings allow write access:
   Settings → Actions → General → Workflow permissions →
   select "Read and write permissions"

---

*Setup guide for Version 0 Research Engine*
*Methodology frozen for 180-day measurement window*
