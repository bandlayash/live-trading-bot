# live-trading-bot

A live-money options bot that trades TQQQ bull call spreads on a mean-reversion
signal. It runs on GitHub Actions, polls every 5 minutes during market hours,
and submits orders through Alpaca's API.

This is the live-money counterpart to my
[`trading-bot`](https://github.com/bandlayash/trading-bot) repo, which runs the
same strategy on a paper Alpaca account via AWS Lambda + EventBridge. That bot
is unchanged and keeps running independently — this one is deliberately
separate so the AWS setup keeps its CloudWatch dashboard and other niceties
untouched.

## Contents

- [How it works in plain English](#how-it-works-in-plain-english)
- [The strategy at a glance](#the-strategy-at-a-glance)
- [The math](#the-math)
  - [RSI: are recent bars more red or more green?](#rsi-are-recent-bars-more-red-or-more-green)
  - [EMA: a moving average that pays more attention to recent prices](#ema-a-moving-average-that-pays-more-attention-to-recent-prices)
  - [Why we need both indicators](#why-we-need-both-indicators)
  - [Bull call spreads: capped upside, capped risk](#bull-call-spreads-capped-upside-capped-risk)
  - [Position sizing](#position-sizing)
  - [Liquidity check: don't pay the bid-ask tax](#liquidity-check-dont-pay-the-bid-ask-tax)
- [Project structure](#project-structure)
- [Running it](#running-it)
- [What the bot logs](#what-the-bot-logs)
- [Risks worth watching](#risks-worth-watching)

---

## How it works in plain English

Every 5 minutes during US market hours, a GitHub Actions cron job spins up a
Linux VM, installs the TA-Lib C library, installs the Python deps, and runs
`src/lambda_function.py`. The script:

1. Asks Alpaca whether the market is open. If not, it logs `market_closed` and
   exits.
2. Pulls the last 60 1-minute bars for TQQQ from Alpaca's IEX feed.
3. Computes two indicators on those bars: **RSI(14)** and **EMA(9)**.
4. If the latest bar's RSI is below 30 *and* price is below EMA9 — and we
   don't already have an open spread — it opens a bull call spread (buy an
   ATM call, sell a slightly OTM call), sized at 40% of equity.
5. If the latest bar's RSI is above 70 *and* price is above EMA9 — and we
   have an open spread — it closes the spread.
6. Otherwise it does nothing and the workflow ends.

There's no portfolio management, no risk-parity, no fancy hedging. It's a
single-pair mean-reversion bot. If TQQQ has dropped enough to look oversold,
make a small directional bet that it bounces. If it's risen enough to look
overbought, take profits.

Why TQQQ? Underlying size is the constraint. With ~$270 of equity and a 40%
risk budget ($108), a single bull call spread must cost ≤ $1.08 net debit.
IWM at ~$215 produced 4%-OTM spreads ~$8 wide, costing ~$3.30/contract —
more than the entire budget. TQQQ at ~$60 produces 4%-OTM spreads ~$2.50
wide, costing $0.80–$1.10 net debit, comfortably within reach. It also has
very liquid weekly options (tight bid-ask < 5% of mid) and 3× leverage
that ensures RSI crosses 30/70 frequently on 1-minute bars — more signals
than the underlying QQQ alone. The mean-reversion logic is unchanged:
RSI < 30 on TQQQ means QQQ is oversold; the spread bets on a bounce.

The bot is a portfolio piece as much as a trading system. At $270 of starting
equity, the goal is to learn the operational mechanics of options trading and
build a clean engineering pipeline. Profits would be nice; uptime, correctness,
and observability matter more.

---

## The strategy at a glance

| Knob               | Value                                | Meaning                                  |
|--------------------|--------------------------------------|------------------------------------------|
| Underlying         | TQQQ                                 | ProShares UltraPro QQQ (3× leveraged)   |
| Bar timeframe      | 1 minute                             | We make decisions on minute bars         |
| History window     | 60 bars                              | Last 60 minutes considered per run       |
| RSI period         | 14                                   | Classic Wilder setting                   |
| EMA period         | 9                                    | Short-term trend filter                  |
| BUY trigger        | RSI < 30 AND price < EMA9            | Oversold AND below short-term trend      |
| SELL trigger       | RSI > 70 AND price > EMA9            | Overbought AND above short-term trend    |
| Spread structure   | Bull call: ATM long + ~4% OTM short  | Defined-risk bullish bet                 |
| Days to expiration | 7-14 days                            | Weekly options window                    |
| Risk per trade     | `RISK_PCT * equity`                  | Currently 40% of equity (~$108 budget)   |
| Account equity     | ~$270                                | Starting capital                         |
| Cadence            | Every 5 min                          | GitHub Actions cron, weekdays during RTH |

With $270 of equity and a 40% risk allocation, the trade budget is $108. Bull
call spreads on TQQQ at 7-14 DTE typically cost $0.80–$1.10 net debit per
contract in normal IV. Most signals will fit; the elevated-IV tail (debit
above $1.08) gets filtered out via the `insufficient_budget` path, which is
intentional — high-IV entries carry the most IV-crush risk on the bounce, so
skipping them is risk management, not a missed opportunity.

---

## The math

### RSI: are recent bars more red or more green?

The **Relative Strength Index** is a number between 0 and 100 that asks: over
the last *N* bars, has price been going up more than down, or vice versa?

The recipe in three steps:

1. For each of the last 14 minutes, compute the bar's change:
   `close_today - close_yesterday`. If positive, it's a "gain"; if negative,
   it's a "loss".
2. Average the gains. Separately, average the absolute values of the losses.
   Call them `avg_gain` and `avg_loss`. (TA-Lib uses Wilder's smoothing — an
   exponential average — rather than a plain mean. Small detail, worth knowing
   if you ever want to reproduce the numbers by hand.)
3. Compute:

   ```
   RS  = avg_gain / avg_loss
   RSI = 100 - 100 / (1 + RS)
   ```

What the output means:

- **RSI = 50**: gains and losses balanced.
- **RSI > 70**: gains have been outweighing losses. The rally has been so
  one-sided that the asset is "overbought" — folklore says it's due for a
  pause or pullback.
- **RSI < 30**: losses have outweighed gains. The asset is "oversold" and,
  by the same folklore, due for a bounce.

The 30/70 thresholds are convention, not law. They're the classic Wilder
defaults and they tend to give too many signals in choppy markets and not
enough in trending ones. That's why we don't use RSI alone.

### EMA: a moving average that pays more attention to recent prices

The **Exponential Moving Average** is a weighted average of past prices where
the weights decay exponentially. The newest bar gets the most weight; bars from
a few minutes ago get less; bars from an hour ago get almost none.

The formula is recursive:

```
α              = 2 / (N + 1)
EMA_today      = α × price_today + (1 - α) × EMA_yesterday
```

For EMA9, `α = 2/10 = 0.2`. So today's price gets 20% of the weight; yesterday's
EMA (which was itself 20% based on yesterday's price, 80% on the day before
that's EMA, and so on) gets the remaining 80%.

The practical effect: EMA9 tracks the recent trend snappily. If TQQQ has been
ticking up for the last few minutes, EMA9 climbs with it. If price suddenly
drops, EMA9 lags a few bars but catches up faster than a simple moving average
would.

We use EMA9 as a **filter**: is the price currently below its short-term
trend line, or above?

### Why we need both indicators

RSI on its own gives bad signals in strong trends. If TQQQ is in a sustained
downtrend, RSI can sit below 30 for hours — every "buy when RSI<30" entry just
catches falling knives. Same problem in reverse on the way up.

EMA9 helps. The combined filter "RSI < 30 AND price < EMA9" says: oversold
*and* price is currently in a confirmed downward move (below its own
short-term average). It's not a guarantee, but it cuts some of the obvious
false starts. Symmetric story for the sell side.

I won't pretend this combination is some magic edge — it's the textbook
mean-reversion setup. It works in choppy, range-bound markets and fails in
trending ones. The point at this account size isn't to discover a hedge-fund-
grade signal; it's to run a real strategy end-to-end and see what breaks.

### Bull call spreads: capped upside, capped risk

Instead of buying a single call (cheaper than buying TQQQ outright but still
meaningful at $1-$3 a pop for ATM weeklies), the bot buys a **bull call
spread** — two simultaneous trades:

- **Long leg**: buy 1 call at strike `K1` (around current price, ATM).
- **Short leg**: sell 1 call at strike `K2`, where `K2 > K1` (slightly OTM).

The premium paid for the long leg is partially offset by the premium collected
for the short leg. The difference is the **net debit** — what comes out of
pocket per spread:

```
net_debit = long_call_mid_price - short_call_mid_price
```

We use the mid-point of each leg's bid-ask spread as fair value.

The payoff at expiration:

```
  profit
    │
    │             ___________   ← max profit at and above K2
    │            /
    │           /
    │          /
  0 ├─────────/────────────     ← break-even at K1 + net_debit
    │  loss  
    │ -net_debit                 ← max loss at and below K1
    │
    └────────────────────────→  underlying price
          K1         K2
```

In numbers, per contract:

- **Max profit** (stock at or above `K2`): `(K2 - K1 - net_debit) × 100`.
- **Max loss** (stock at or below `K1`): `net_debit × 100` — you forfeit
  the debit, nothing more.
- **Break-even**: `K1 + net_debit`.

(The `× 100` is because each option contract represents 100 shares of the
underlying.)

The bounded-loss property is the entire point of using a spread instead of a
naked call. The most you can lose is what you paid to open the position. With
a $270 account, that's a feature, not a limitation: one bad trade can't blow
up the whole account.

Strike selection in the code:

- **Long strike**: the contract closest to current TQQQ price (ATM, often
  slightly ITM).
- **Short strike**: the contract whose strike is roughly
  `long_strike + 4% × price` — for TQQQ at ~$60 that's about $2.40 above the
  long leg, which snaps to the nearest available listed strike (typically
  $0.50 or $1 spacing in the relevant band).
- **Expiration**: the nearest available between 7 and 14 days out
  (usually the upcoming weekly).

### Position sizing

Once we have a candidate spread and a net debit, we compute how many contracts
to buy:

```
budget = equity × RISK_PCT
qty    = floor(budget / (net_debit × 100))
```

With current settings (equity ≈ $270, `RISK_PCT = 0.4`):

```
budget = 270 × 0.4 = $108

net_debit = $0.60 → cost = $60   → qty = floor(108/60)  = 1
net_debit = $1.00 → cost = $100  → qty = floor(108/100) = 1
net_debit = $1.20 → cost = $120  → qty = floor(108/120) = 0 (skip)
net_debit = $2.00 → cost = $200  → qty = floor(108/200) = 0 (skip)
```

The `floor` matters — we never buy a fractional contract. If the budget can't
fit even one, the bot returns `insufficient_budget` and waits for the next
signal.

### Liquidity check: don't pay the bid-ask tax

Before committing to a spread, the code rejects either leg if its bid-ask
spread is more than 20% of the mid price:

```python
bid_ask_width = ask - bid
if bid_ask_width / mid > 0.20:
    return None  # skip this trade
```

Why bother: an option quoted at $1.00 mid with a $0.50 bid-ask gap (so $0.75
bid, $1.25 ask) means you'd likely pay $1.25 on entry and sell at $0.75 on
exit, eating a $0.50 round-trip cost on a $1.00 position. That's 50% of the
trade lost to spread cost alone, before any market move. Liquid TQQQ weeklies
usually have tight spreads, but the check is cheap insurance against the
occasional thin strike.

---

## Project structure

```
live_trading_bot/
├── .github/
│   └── workflows/
│       └── bot.yml             # GitHub Actions workflow + manual trigger
├── src/
│   └── lambda_function.py      # All strategy + Alpaca client code
├── .gitignore
├── README.md                   # You're reading it
└── requirements.txt            # alpaca-py, pandas, numpy, ta-lib
```

### `.github/workflows/bot.yml`

Defines the scheduled job:

- **Trigger**: `workflow_dispatch` only — scheduling is handled externally by
  cron-job.org, which fires the workflow every 5 minutes during market hours
  via the GitHub REST API. The in-code `trading_client.get_clock()` check
  narrows further to actual regular trading hours.
- **Kill switch**: `if: ${{ vars.LIVE_ENABLED == 'true' }}` — the job is gated
  on a repo Variable. Set `LIVE_ENABLED` to anything other than `true` (or
  delete it) and every invocation, cron or manual, skips immediately. No
  commit, no redeploy.
- **Manual trigger**: `workflow_dispatch` is enabled for smoke testing.
- **Runtime**: GitHub-hosted Ubuntu runner. Installs the TA-Lib C library via
  its official `.deb` from
  [ta-lib/ta-lib releases](https://github.com/ta-lib/ta-lib/releases) (Ubuntu
  doesn't ship `libta-lib0` in default repos), then installs Python deps and
  runs the bot.

### `src/lambda_function.py`

One file, no helper modules. Top-to-bottom layout:

- **Imports + setup** — Alpaca clients, env vars (`ALPACA_KEY`,
  `ALPACA_SECRET`, `ALPACA_PAPER`, `SYMBOLS`, `RISK_PCT`, `MINUTES_HISTORY`),
  TA-Lib, pandas.
- **Logging** — root logger + `logging.basicConfig` so `logger.info(...)`
  lines surface in Actions stderr.
- **`compute_indicators`** — wraps TA-Lib's `RSI` and `EMA` into a DataFrame.
- **`fetch_minute_bars`** — pulls the last *N* minute bars from Alpaca's IEX
  feed.
- **`get_portfolio_equity`** — reads account equity.
- **`select_spread_contracts`** — finds the long/short option pair for a given
  underlying and current price.
- **`get_spread_quote`** — mid-prices the spread, runs the liquidity check.
- **`get_option_positions`** — looks up any open spread on the underlying.
- **`open_bull_call_spread` / `close_bull_call_spread`** — submit MLEG limit
  orders to Alpaca.
- **`evaluate_and_trade`** — the strategy loop. Pulls bars, computes
  indicators, dispatches BUY / SELL / no-signal.
- **`lambda_handler`** — entrypoint. Market-hours gate, per-symbol loop. Kept
  the name because the file was an AWS Lambda handler originally; it's now
  invoked from `__main__` as a CLI.
- **`__main__`** — calls `lambda_handler({}, None)` so `python
  src/lambda_function.py` does the same thing the workflow does.

A note on the filename `src/lambda_function.py`: this codebase grew out of an
AWS Lambda deployment, and `lambda_function.py` was the Lambda entry point.
It's no longer a Lambda — it's a CLI script invoked by GitHub Actions — but I
kept the name to minimize churn from the original handoff document. Mentally
substitute "bot.py" if it bothers you.

### `requirements.txt`

```
alpaca-py>=0.13.0   # Alpaca's official Python SDK
pandas              # data manipulation
numpy               # used via talib + pandas .values arrays
ta-lib==0.6.8       # technical indicators (RSI, EMA)
```

The `ta-lib` Python package is just bindings — the underlying TA-Lib C library
is installed separately in the workflow before pip runs.

---

## Running it

### Required GitHub configuration

**Secrets** (Settings → Secrets and variables → Actions → Secrets tab):

| Name                 | Value                            |
|----------------------|----------------------------------|
| `LIVE_ALPACA_KEY`    | Live Alpaca trading key ID       |
| `LIVE_ALPACA_SECRET` | Live Alpaca trading secret       |

**Variables** (Variables tab — not Secrets):

| Name           | Value                                                            |
|----------------|------------------------------------------------------------------|
| `LIVE_ENABLED` | `true` to arm the bot; unset or anything else pauses it          |

`LIVE_ENABLED` is the kill switch. Flipping it to anything other than `true`
(or deleting the variable) makes every job invocation skip immediately.

### Local development

Set up an environment with TA-Lib's C library available. The simplest path on
Windows is conda:

```powershell
conda create -n live-trading-bot python=3.11 -c conda-forge ta-lib -y
conda activate live-trading-bot
pip install -r requirements.txt
```

Run against paper (safe — uses the paper Alpaca account, doesn't touch real
money):

```powershell
$env:ALPACA_KEY = "<paper-key>"
$env:ALPACA_SECRET = "<paper-secret>"
$env:ALPACA_PAPER = "true"
$env:SYMBOLS = "TQQQ"
$env:RISK_PCT = "0.05"
$env:MINUTES_HISTORY = "60"
python src/lambda_function.py
```

Run against live with no-trade safety (live account, but `RISK_PCT=0.0` forces
`insufficient_budget` so no order is submitted — useful for verifying
credentials and equity reads end-to-end):

```powershell
$env:ALPACA_KEY = "<live-key>"
$env:ALPACA_SECRET = "<live-secret>"
$env:ALPACA_PAPER = "false"
$env:RISK_PCT = "0.0"
python src/lambda_function.py
```

---

## What the bot logs

Every run prints structured JSON events on stdout — one event per line, easy
to grep:

- `{"event": "market_closed", "next_open": "..."}` — fired when the run starts
  outside regular trading hours. The handler exits immediately; no positions
  are touched.
- `{"event": "run_started", "mode": "live"|"paper", "equity": ..., "symbols": [...]}`
  — per-run heartbeat with portfolio equity. Useful for tracking equity over
  time.
- `{"event": "trade_opened", "symbol": ..., "long": ..., "short": ..., "qty": ..., "net_debit": ..., "spread_debit_total": ..., "equity": ..., "order_id": ...}`
  — emitted after submitting a BUY spread.
- `{"event": "trade_closed", "symbol": ..., "long": ..., "short": ..., "qty": ..., "limit_credit": ..., "spread_credit_total": ..., "pnl": ..., "order_id": ...}`
  — emitted after submitting a SELL spread.

`logger.info(...)` lines (formatted by `logging.basicConfig`) surface on
stderr — diagnostic context like bar close, RSI value, EMA value, contract
selection.

GitHub's Actions UI lets you search inside a single run's logs but doesn't
aggregate across runs. For longitudinal analysis you'd want to export logs via
the GitHub API or pipe trade events to an external sink. Out of scope for
this version.

---

## Risks worth watching

- **Pattern Day Trader rule.** US options accounts under $25k that open and
  close 3+ same-day round trips in 5 business days get flagged as PDT, and new
  opens are blocked until the account is funded over $25k or the rolling
  5-day window clears. This strategy can fire that often on choppy days.
  Watch logs for `pattern_day_trader` in rejection messages.
- **Settled cash for options.** Options trades require settled cash, not just
  buying power. ACH deposits take T+1 to T+2 to settle. Check the "Settled
  cash" balance in Alpaca before assuming the equity number is fully
  tradeable.
- **Live vs paper fill quality.** Paper fills happen at mid-price; live fills
  happen at whatever the market gives you. Expect 5-15% worse PnL than paper
  for comparable trades over a meaningful sample. If the gap is bigger than
  that, the cause is usually Actions cron delay (signal computed on stale
  bars).
- **Actions cron is best-effort.** GitHub schedules can be delayed 5-15
  minutes during platform load and very occasionally skipped. EventBridge
  (the sibling bot's AWS scheduler) typically dispatches within 30 seconds.
  For a 5-min poller this means occasional missed ticks and stale-data
  signals. Acceptable at this scale, would not be at larger size.
- **60-day auto-disable.** GitHub auto-disables scheduled workflows on repos
  with no activity for 60 days. Push something at least every 2 months or
  accept the manual re-enable.
- **Order acceptance ≠ order fill.** `open_bull_call_spread` returns when
  Alpaca acknowledges submission, not when the order fills. A `trade_opened`
  log doesn't guarantee a position. If the next tick's `get_option_positions`
  returns empty, the bot will (correctly) try again. Worth knowing if you're
  watching logs in real time.

---

The sibling [`trading-bot`](https://github.com/bandlayash/trading-bot) repo
continues to run the same strategy on a paper Alpaca account via AWS Lambda +
EventBridge. The two bots execute the same logic but on different schedules
and different accounts, so their decisions and PnL will diverge — that's
expected, not a bug.
