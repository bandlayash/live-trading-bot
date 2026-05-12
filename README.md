# live-trading-bot

A live-money RSI/EMA bull-call-spread bot for QQQ, scheduled by GitHub Actions
cron every 5 minutes during market hours.

Sibling project [`trading-bot`](https://github.com/bandlayash/trading-bot)
runs the same strategy on a paper Alpaca account via AWS Lambda +
EventBridge — that bot is unchanged and continues to operate independently.
This repo is the live counterpart, deliberately separate so the AWS setup
keeps its CloudWatch dashboard and ybandla.com equity feed untouched.

See [`handoff.md`](./handoff.md) for the full context, tradeoffs, and
operational runbook.

## Strategy

- Underlying: QQQ
- Indicators: RSI(14) and EMA(9) on 1-minute IEX bars
- BUY signal: RSI < 30 AND price < EMA9 → open a 7-14 DTE bull call spread
  (ATM long + ~$10 OTM short), sized to `RISK_PCT` of portfolio equity
- SELL signal: RSI > 70 AND price > EMA9 → close the open spread
- Market-hours gate: in-code `trading_client.get_clock()` check
  short-circuits outside Alpaca's reported regular trading session

## Required GitHub configuration

**Secrets** (Settings → Secrets and variables → Actions → Secrets):

| Name                 | Value                            |
|----------------------|----------------------------------|
| `LIVE_ALPACA_KEY`    | Live Alpaca trading key ID       |
| `LIVE_ALPACA_SECRET` | Live Alpaca trading secret       |

**Variables** (Variables tab, **not** Secrets):

| Name           | Value                                                            |
|----------------|------------------------------------------------------------------|
| `LIVE_ENABLED` | `"true"` to arm the bot; unset or any other value disables it    |

`LIVE_ENABLED` is a one-click kill switch — the workflow's `if:` condition
checks it before the live job runs, so flipping it off pauses the bot
without touching code.

## Local development

```powershell
conda create -n live-trading-bot python=3.11 -c conda-forge ta-lib -y
conda activate live-trading-bot
pip install -r requirements.txt
```

Run against paper (safe):

```powershell
$env:ALPACA_KEY = "<paper-key>"
$env:ALPACA_SECRET = "<paper-secret>"
$env:ALPACA_PAPER = "true"
$env:SYMBOLS = "QQQ"
$env:RISK_PCT = "0.05"
$env:MINUTES_HISTORY = "60"
python src/lambda_function.py
```

Run against live with no-trade safety (RTH only, `RISK_PCT=0.0` forces
`insufficient_budget` so no order is submitted — confirms creds and equity
read without risking a trade):

```powershell
$env:ALPACA_KEY = "<live-key>"
$env:ALPACA_SECRET = "<live-secret>"
$env:ALPACA_PAPER = "false"
$env:RISK_PCT = "0.0"
python src/lambda_function.py
```

## Output

Structured JSON events (one per line) on stdout:

- `{"event": "market_closed", "next_open": "..."}` — fired when run starts
  outside regular trading hours; handler exits immediately.
- `{"event": "run_started", "mode": "live"|"paper", "equity": ..., "symbols": [...]}` —
  per-run heartbeat with portfolio equity.
- `{"event": "trade_opened", ...}` — emitted after submitting a BUY spread.
- `{"event": "trade_closed", ...}` — emitted after submitting a SELL spread.

`logger.info(...)` lines (formatted via `logging.basicConfig`) appear on
stderr for indicator/diagnostic context.
