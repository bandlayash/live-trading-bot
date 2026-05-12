# Trading Bot Handoff: AWS → GitHub Actions

This document is the migration plan for moving `bandlayash/trading-bot` from AWS
Lambda + EventBridge to GitHub Actions, while adding live Alpaca trading
alongside the existing paper bot.

## Current state (before this migration)

- `src/lambda_function.py` runs the strategy. Triggered by EventBridge every
  5 min during market hours via `template.yaml`.
- `paper=True` is hardcoded in the `TradingClient` initialization.
- One Alpaca account in scope (paper). Real-time stats published on
  ybandla.com/projects via CloudWatch.
- Live Alpaca account is now approved for Level 3 options. Funded with ~$270.

## Target state (after this migration)

- Two parallel runs of the same strategy, controlled by env vars:
  - **paper**: existing strategy, existing paper account, `RISK_PCT=0.05`
  - **live**: same code, live account, `RISK_PCT=0.5` initially
- Both triggered by a single GitHub Actions workflow on `cron`.
- AWS resources (Lambda, EventBridge, CloudWatch dashboard) decommissioned
  after parity is confirmed.
- Optional: dashboard endpoint kept on a tiny standalone service or rewired
  to read directly from Alpaca's API client-side.

## Honest tradeoffs you're accepting by leaving AWS

Read these before starting. None are dealbreakers at this account size, but
they're real.

1. **GitHub Actions cron is best-effort**, not guaranteed. Schedules can be
   delayed 5–15 minutes during platform load and very occasionally skipped.
   EventBridge is typically <30s. For a 5-min poller on a mean-reversion
   signal, occasional delays will cause stale-data signals and degraded fills
   vs. paper. Acceptable here, would not be acceptable at scale.
2. **Cold start per run**: ~30–60s of VM provisioning + dependency install
   per invocation (drops to ~10s with pip caching, which the workflow below
   uses). Lambda warm starts were much faster.
3. **Logs retained 90 days** in Actions vs. configurable (effectively
   indefinite) in CloudWatch. Export anything you need to keep long-term.
4. **Scheduled workflows auto-disable after 60 days of repo inactivity.**
   Either commit something periodically or accept the manual re-enable.
5. **No native metrics/alerts.** Run status is a green/red check; anything
   richer needs to be exported elsewhere or written into the repo (ugly).
6. **No native timezone support in cron.** Workflow runs in UTC. The bot's
   in-code market-hours check handles the actual gating, so cron just needs
   to fire wide enough to cover both EST and EDT.

If any of those are dealbreakers, stay on AWS. Add a second Lambda for live
alongside the existing paper one, change `paper` to read from an env var,
done.

---

## Step 1: Refactor `src/lambda_function.py` to be env-driven and CLI-runnable

Two changes:

### 1a. Read `paper` from an environment variable

Find:

```python
trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
```

Replace with:

```python
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
trading_client = TradingClient(API_KEY, API_SECRET, paper=ALPACA_PAPER)
```

Default is `"true"` so missing-env-var fails safe (paper, never live).

### 1b. Make the file directly executable

At the bottom of the file, after the existing `lambda_handler(event, context)`
definition, add:

```python
if __name__ == "__main__":
    import json
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
```

This lets `python src/lambda_function.py` produce the same behavior as a
Lambda invocation. The empty event dict is fine; the handler doesn't read
anything from `event` for the trading path (the only event-reading code is
the dashboard endpoint, which we're removing from this flow — see Step 4).

### 1c. Remove or guard CloudWatch metric publishing

Anywhere the code calls `boto3` or publishes CloudWatch metrics, either
delete those calls or wrap them in:

```python
if os.environ.get("PUBLISH_METRICS", "false").lower() == "true":
    publish_metric(...)
```

GitHub Actions doesn't have AWS credentials by default, and the boto3 calls
will fail or hang. Easiest: delete the metric publishing code paths entirely
for the GitHub Actions migration. Add a structured log line instead so the
data is still in the run logs:

```python
print(json.dumps({
    "event": "trade_opened",
    "symbol": symbol,
    "qty": qty,
    "net_debit": net_debit,
    "mode": "live" if not ALPACA_PAPER else "paper",
}))
```

---

## Step 2: Update `requirements.txt`

The Lambda version probably pins to manylinux wheels and uses a Layer for
talib. On GitHub Actions runners (Ubuntu), you install talib's system library
separately. So:

**`requirements.txt`** (Python deps only):

```
alpaca-py>=0.13.0
pandas
numpy
ta-lib==0.4.32
```

The `ta-lib` Python wheel needs the system library installed first. The
workflow installs that with `apt-get` before `pip install`.

---

## Step 3: Create `.github/workflows/bot.yml`

Replace the existing `deploy.yml` SAM deployment workflow with this. The old
SAM deploy workflow can be deleted — it's deploying to infrastructure you're
about to tear down.

```yaml
name: trading-bot

on:
  schedule:
    # Every 5 minutes, Mon-Fri, 13:30-21:00 UTC.
    # That window covers 9:30-16:00 ET during EDT (most of the year)
    # AND 8:30-15:00 ET during EST. The in-code market-hours check
    # gates actual trading to the real session. Cron just needs to fire
    # widely enough to never miss the session due to DST.
    - cron: '*/5 13-21 * * 1-5'
  workflow_dispatch:    # manual trigger for smoke tests

# Don't pile up runs if one is slow. If a tick is mid-flight when the next
# is scheduled, cancel the older one. For a stateless bot this is fine.
concurrency:
  group: trading-bot
  cancel-in-progress: false

jobs:
  paper:
    runs-on: ubuntu-latest
    timeout-minutes: 4
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install talib system library
        run: |
          sudo apt-get update
          sudo apt-get install -y libta-lib0 libta-lib-dev

      - name: Install Python dependencies
        run: pip install -r requirements.txt

      - name: Run bot (paper)
        env:
          ALPACA_KEY: ${{ secrets.PAPER_ALPACA_KEY }}
          ALPACA_SECRET: ${{ secrets.PAPER_ALPACA_SECRET }}
          ALPACA_PAPER: "true"
          SYMBOLS: "QQQ"
          RISK_PCT: "0.05"
          MINUTES_HISTORY: "60"
        run: python src/lambda_function.py

  live:
    runs-on: ubuntu-latest
    timeout-minutes: 4
    # Comment this `if` out to enable live; leave it on `false` until you've
    # verified paper runs cleanly on Actions for at least a day.
    if: ${{ vars.LIVE_ENABLED == 'true' }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install talib system library
        run: |
          sudo apt-get update
          sudo apt-get install -y libta-lib0 libta-lib-dev

      - name: Install Python dependencies
        run: pip install -r requirements.txt

      - name: Run bot (live)
        env:
          ALPACA_KEY: ${{ secrets.LIVE_ALPACA_KEY }}
          ALPACA_SECRET: ${{ secrets.LIVE_ALPACA_SECRET }}
          ALPACA_PAPER: "false"
          SYMBOLS: "QQQ"
          RISK_PCT: "0.5"
          MINUTES_HISTORY: "60"
        run: python src/lambda_function.py
```

### Notes on this workflow

- The `live` job is gated on a repo **variable** `LIVE_ENABLED`. Default
  state should be unset (live disabled) until you've watched paper run on
  Actions for at least a market day. Set it to `"true"` in Settings →
  Secrets and variables → Actions → Variables tab when ready to go live.
  This gives you a one-click kill switch.
- `concurrency: cancel-in-progress: false` means if a tick is still running
  when the next cron fires, the new one queues rather than killing the old.
  At 5-min cadence with ~30s runs, you should never actually queue.
- `timeout-minutes: 4` protects against a hung run blocking the next tick.
- Both jobs run in parallel within the same workflow trigger, so paper and
  live use the same minute's bar data. Good for apples-to-apples comparison.

---

## Step 4: Decide what to do with the dashboard endpoint

Your current Lambda doubles as an HTTP endpoint that returns portfolio
history JSON to ybandla.com/projects. GitHub Actions can't serve HTTP — it
only runs jobs on schedule or on push.

Three options:

**A. Leave a tiny Lambda just for the dashboard.** Keep the existing
function but strip out the trading code path. EventBridge rule deleted; only
the API Gateway / function URL remains. Costs ~$0/mo at low traffic. This is
the cleanest split: Actions runs the strategy, Lambda serves the read-only
endpoint. Update the SAM `template.yaml` to remove the schedule rule and
keep only the function + URL.

**B. Migrate the endpoint to Cloudflare Workers / Vercel / Netlify
Functions.** Free tiers easily cover this. Rewrite the handler in JS or use
Python on Vercel.

**C. Move portfolio fetching client-side in ybandla.com.** Skip the proxy
entirely. The Alpaca data client can run from the browser if you generate a
read-only API key and accept the security tradeoff that the key is visible
to anyone who views your site's network tab. **Don't do this** unless you
make a separate read-only key and confirm Alpaca's terms allow it; default
keys have full account access.

My recommendation: **option A**. Smallest delta from current setup, keeps the
boundary clean.

---

## Step 5: Set up GitHub secrets

In the repo: Settings → Secrets and variables → Actions → New repository
secret.

Add four secrets:

| Name                 | Value                                  |
|----------------------|----------------------------------------|
| `PAPER_ALPACA_KEY`   | Your existing paper trading key ID     |
| `PAPER_ALPACA_SECRET`| Your existing paper trading secret     |
| `LIVE_ALPACA_KEY`    | Your live trading key ID               |
| `LIVE_ALPACA_SECRET` | Your live trading secret               |

Add one variable (Variables tab, **not** Secrets):

| Name           | Value                                       |
|----------------|---------------------------------------------|
| `LIVE_ENABLED` | (leave unset until paper is validated)      |

---

## Step 6: Initial cutover

Order matters. Do not skip steps.

1. **Code changes in a branch.** Make the changes from Steps 1–3 in a feature
   branch. Push. Confirm the workflow lints (Actions tab → workflow appears).
2. **Manually run paper once.** Actions → trading-bot → Run workflow →
   select the branch → run. Click into the run logs and confirm:
   - talib installs
   - dependencies install (cached after first run)
   - Bot logs in to Alpaca paper, fetches bars, evaluates signal, prints
     `action: no_signal` or similar
   - Total run time: should be <60s after first cached run
3. **Merge to main.** The cron schedule activates on `main`.
4. **Wait for the next scheduled run.** Watch the Actions tab and confirm
   it fires within ~1 minute of the cron time.
5. **Disable the AWS EventBridge rule.** Go to AWS Console → EventBridge →
   your rule → Disable. **Don't delete yet** — you might need to roll back.
   At this point, paper is running on Actions and not on AWS.
6. **Run paper-only on Actions for at least one full trading day.** Watch
   for: missed runs, late runs, auth errors, talib install failures, any
   divergence from what your Lambda was doing at the same minute.
7. **Enable live.** Set the `LIVE_ENABLED` variable to `"true"` in repo
   Variables. Next cron tick, the live job will fire too.
8. **Monitor the first live trade.** When the first BUY signal fires, open
   the Alpaca live dashboard immediately and watch the order route, fill,
   and post-fill state. Confirm legs/strikes/qty match what the log says.

---

## Step 7: Decommission AWS (after 1–2 weeks of clean operation)

Once Actions has been running both paper and live for a couple of weeks
without issues:

1. Delete the EventBridge rule (was disabled in step 6.5).
2. If you went with **option A** for the dashboard, update `template.yaml` to
   remove the EventBridge resource and any IAM permissions related to
   scheduling. Keep the function + URL. Redeploy via your existing SAM
   pipeline.
3. If you went with **option B** or **C**, delete the entire Lambda
   function, CloudWatch log groups, CloudWatch dashboard, and the SAM stack.
4. Update your README to reflect the new architecture.
5. Delete `deploy.yml` (the SAM deploy workflow) from `.github/workflows/`.

---

## Step 8: Observability for Actions-based bot

Since you're losing CloudWatch metrics and dashboards, set up the bare
minimum to know if the bot is healthy:

1. **Email on failure.** Settings → Notifications → Actions → "Send
   notifications for failed workflows only." Default is on for the repo
   owner — confirm it's enabled.
2. **Structured log lines.** Per Step 1c, make every meaningful event print
   a JSON line. The Actions log viewer can search across runs.
3. **Optional: weekly export.** A second workflow that runs every Sunday,
   downloads the last week of run logs via the GitHub API, and commits a
   summary CSV to a `logs/` directory in the repo. Keeps a permanent record
   beyond the 90-day Actions retention.
4. **Optional: Discord/Slack webhook on trade.** Add a step at the end of
   each job that POSTs to a webhook if the run output indicates a trade
   was opened or closed. Useful for paying attention without staring at
   the Actions tab.

---

## Step 9: `template.yaml` changes

If you took **option A** (keep Lambda for the dashboard endpoint only),
the new `template.yaml` should:

- Remove the `Events:` block on the function (no more EventBridge schedule)
- Remove the `Schedule` parameter if you had one
- Keep the function definition, the dependency layer, and any
  `FunctionUrlConfig` for the dashboard endpoint
- Remove CloudWatch dashboard resources (they were dashboarding the
  scheduled runs that no longer happen)
- Trim env vars to only those the dashboard endpoint needs (probably just
  `ALPACA_KEY` / `ALPACA_SECRET`)

If you took **option B** or **option C**, delete `template.yaml` and the
SAM stack entirely (`sam delete --stack-name <name>`).

---

## Recommended starting parameters for live

Repeating these here so they're in one place:

```
ALPACA_PAPER=false
RISK_PCT=0.5         # not 1.0 — see reasoning below
SYMBOLS=QQQ
MINUTES_HISTORY=60
```

`RISK_PCT=0.5` on $270 equity gives a $135/trade budget. Typical 7-14 DTE
QQQ bull call spread debits run $100–200, so you'll get 0–1 contracts per
signal. Half your signals won't trade. That's fine for week 1: it biases
toward cheaper (lower-IV) signals and leaves capital for a second attempt
after a loss. After 2+ weeks of clean execution, bump to `RISK_PCT=1.0`
if confident.

---

## Known issues to watch for

- **Pattern Day Trader rule.** Account under $25k + 3 same-day open+close
  spread cycles in 5 business days = account flagged, new opens blocked.
  This strategy can fire that often on a choppy day. If it happens, you'll
  see order rejections with `pattern_day_trader` in the error. Either wait
  out the rolling 5-day window or live with closing-only mode until you
  fund up to $25k.
- **Settled cash vs. instant deposit.** Options trades require settled
  cash. ACH deposits take T+1 to T+2 to settle. If you just funded the
  account, check Balances → "Settled cash" before going live.
- **First live trade may surprise you.** Paper fills at mid-price; live
  fills at whatever the market gives you. Expect 5–15% worse PnL than
  paper for comparable trades over a meaningful sample. If the gap is
  much bigger than that, you have an execution problem (often: late
  signals due to Actions cron delay).
- **Workflow auto-disable at 60 days idle.** Push at least one commit per
  60 days, or accept the re-enable.

---

## Rollback plan

If GitHub Actions causes problems you don't want to live with:

1. Re-enable the EventBridge rule in AWS (you disabled it, didn't delete it,
   in Step 6).
2. Revert the `paper=...` change in `lambda_function.py` if you removed the
   hardcoded value.
3. Delete or disable `.github/workflows/bot.yml`.
4. AWS resumes running paper at the next scheduled tick.

This rollback is clean as long as you didn't tear down AWS in Step 7 yet.
The two-week wait before decommissioning AWS exists specifically for this.

---

## Open questions for future you

- Whether to add more symbols beyond QQQ once live is stable. The signal
  is generic; more symbols = more shots on goal but also more PDT risk.
- Whether the 5-min cadence is still right given Actions' less-precise
  scheduling. Could explore 10-min cadence to reduce sensitivity to delays.
- Whether to move to a small always-on VM (Oracle, Fly.io free tier,
  Railway, etc.) if Actions reliability becomes a real problem.
- Whether to add a stop-loss or trend filter to the strategy itself.
  Mean-reversion without a regime filter is the textbook small-account
  killer when the market trends hard.
