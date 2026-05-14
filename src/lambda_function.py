from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    OptionLegRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    OrderClass,
    ContractType,
    AssetStatus,
)
from datetime import datetime, timedelta, timezone, date
from math import floor
import logging
import pandas as pd
import os
import talib
import json

ALPACA_KEY = os.environ["ALPACA_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# Trading client (paper vs live via ALPACA_PAPER env var)
trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
SYMBOLS = os.environ.get('SYMBOLS').split(",")
RISK_PCT = float(os.environ.get('RISK_PCT'))
MINUTES_HISTORY = int(os.environ.get('MINUTES_HISTORY'))

# Setting up data clients
data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
option_data_client = OptionHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

# logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def compute_indicators(close_series: pd.Series):
    """
    Calculates RSI(14) and EMA(9) using talib and stores in pandas df
    returns: DataFrame with columns: close, rsi14, ema9
    """

    df = pd.DataFrame({"close": close_series})
    df["rsi14"] = talib.RSI(df["close"].values, timeperiod=14)
    df["ema9"] = talib.EMA(df["close"].values, timeperiod=9)
    return df

# Data fetching

def fetch_minute_bars(symbol: str, minutes: int) -> pd.DataFrame:
    """
    Fetch the last `minutes` minute-bars (close prices) up to now (ET market time)
    returns: pandas Series indexed by timestamp
    """

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes*2)
    req = StockBarsRequest(symbol_or_symbols=[symbol], start=start.isoformat(), end=end.isoformat(),timeframe=TimeFrame.Minute, feed="iex")
    bars = data_client.get_stock_bars(req)
    df = bars.df

    if symbol not in df.index.get_level_values(0):
        return pd.DataFrame()

    df_sym = df.xs(symbol, level = 0)
    df_sym = df_sym.sort_index()
    if len(df_sym) > minutes:
        df_sym = df_sym.iloc[-minutes:]
    return df_sym.tz_convert("UTC")

# Account and orders

def get_portfolio_equity():
    """
    Helper function to get portfolio equity
    """
    acct = trading_client.get_account()
    return float(acct.equity)

# Options helpers

def select_spread_contracts(underlying: str, current_price: float):
    """
    Find ATM long call and ~3-5% OTM short call for a bull call spread.
    Targets the nearest weekly expiration 7-14 days out.
    Returns (long_symbol, short_symbol) or None if no suitable contracts.
    """
    today = date.today()
    exp_min = today + timedelta(days=7)
    exp_max = today + timedelta(days=14)

    # Fetch available call contracts in the strike/expiration window
    req = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        strike_price_gte=str(int(current_price * 0.97)),
        strike_price_lte=str(int(current_price * 1.08)),
        expiration_date_gte=exp_min,
        expiration_date_lte=exp_max,
        limit=100,
    )
    resp = trading_client.get_option_contracts(req)
    contracts = resp.option_contracts if resp.option_contracts else []

    if len(contracts) < 2:
        logger.warning("Not enough option contracts for %s spread", underlying)
        return None

    # Group by expiration, pick the earliest available
    by_exp = {}
    for c in contracts:
        by_exp.setdefault(c.expiration_date, []).append(c)

    target_exp = min(by_exp.keys())
    available = sorted(by_exp[target_exp], key=lambda c: float(c.strike_price))

    # Long leg: strike closest to current price (ATM), preferring at-or-below
    long_contract = min(available, key=lambda c: abs(float(c.strike_price) - current_price))
    long_strike = float(long_contract.strike_price)

    target_short_strike = long_strike + current_price * 0.04
    candidates = [c for c in available if float(c.strike_price) > long_strike]
    if not candidates:
        logger.warning("No OTM strikes available above %.2f for %s", long_strike, underlying)
        return None

    short_contract = min(candidates, key=lambda c: abs(float(c.strike_price) - target_short_strike))

    logger.info(
        "Selected spread: long %s (strike %.2f) / short %s (strike %.2f), exp %s",
        long_contract.symbol, long_strike,
        short_contract.symbol, float(short_contract.strike_price),
        target_exp,
    )
    return long_contract.symbol, short_contract.symbol


def get_spread_quote(long_symbol: str, short_symbol: str):
    """
    Get mid-price quotes for both legs and return net debit.
    Returns (net_debit, long_mid, short_mid) or None if quotes unavailable.
    """
    req = OptionLatestQuoteRequest(symbol_or_symbols=[long_symbol, short_symbol])
    quotes = option_data_client.get_option_latest_quote(req)

    long_q = quotes.get(long_symbol)
    short_q = quotes.get(short_symbol)
    if not long_q or not short_q:
        logger.warning("Could not get quotes for %s / %s", long_symbol, short_symbol)
        return None

    long_mid = (float(long_q.bid_price) + float(long_q.ask_price)) / 2
    short_mid = (float(short_q.bid_price) + float(short_q.ask_price)) / 2

    if long_mid <= 0 or short_mid <= 0:
        logger.warning("Invalid quote prices: long=%.4f short=%.4f", long_mid, short_mid)
        return None

    # Reject if bid-ask spread is too wide (> 20% of mid) on either leg
    for sym, q, mid in [(long_symbol, long_q, long_mid), (short_symbol, short_q, short_mid)]:
        bid_ask_width = float(q.ask_price) - float(q.bid_price)
        if mid > 0 and bid_ask_width / mid > 0.20:
            logger.warning("Bid-ask too wide on %s: %.2f vs mid %.2f", sym, bid_ask_width, mid)
            return None

    net_debit = round(long_mid - short_mid, 2)
    if net_debit <= 0:
        logger.warning("Net debit is non-positive: %.2f", net_debit)
        return None

    return net_debit, long_mid, short_mid


def get_option_positions(underlying: str):
    """
    Return list of open option positions for the given underlying symbol.
    """
    all_positions = trading_client.get_all_positions()
    return [p for p in all_positions if p.symbol.startswith(underlying) and "C" in p.symbol]


def open_bull_call_spread(long_symbol: str, short_symbol: str, qty: int, limit_price: float):
    """
    Submit an MLEG limit order to open a bull call spread.
    """
    legs = [
        OptionLegRequest(symbol=long_symbol, ratio_qty=1, side=OrderSide.BUY),
        OptionLegRequest(symbol=short_symbol, ratio_qty=1, side=OrderSide.SELL),
    ]
    order_req = LimitOrderRequest(
        order_class=OrderClass.MLEG,
        qty=qty,
        legs=legs,
        limit_price=limit_price,
        time_in_force=TimeInForce.DAY,
    )
    return trading_client.submit_order(order_data=order_req)


def close_bull_call_spread(long_symbol: str, short_symbol: str, qty: int, limit_price: float):
    """
    Submit an MLEG limit order to close a bull call spread.
    """
    legs = [
        OptionLegRequest(symbol=long_symbol, ratio_qty=1, side=OrderSide.SELL),
        OptionLegRequest(symbol=short_symbol, ratio_qty=1, side=OrderSide.BUY),
    ]
    order_req = LimitOrderRequest(
        order_class=OrderClass.MLEG,
        qty=qty,
        legs=legs,
        limit_price=limit_price,
        time_in_force=TimeInForce.DAY,
    )
    return trading_client.submit_order(order_data=order_req)


# Strategy

def evaluate_and_trade(symbol: str):
    """
    Sends a buy or sell signal depending on indicators
    """
    df_bars = fetch_minute_bars(symbol, MINUTES_HISTORY)
    if df_bars.empty or "close" not in df_bars.columns:
        return {"symbol": symbol, "action": "no_data"}

    close = df_bars["close"].astype(float)
    indicators = compute_indicators(close).dropna()
    if indicators.empty:
        return {"symbol": symbol, "action": "insufficient_data"}

    last = indicators.iloc[-1]
    c = float(last["close"])
    rsi = float(last["rsi14"])
    ema9 = float(last["ema9"])

    logger.info("%s -> close=%.4f rsi=%.2f ema9=%.4f", symbol, c, rsi, ema9)

    # BUY — open a bull call spread
    if rsi < 30 and c < ema9:
        # Skip if we already have an open spread
        existing = get_option_positions(symbol)
        if existing:
            logger.info("Already have %d option position(s) on %s, skipping buy", len(existing), symbol)
            return {"symbol": symbol, "action": "spread_already_open"}

        contracts = select_spread_contracts(symbol, c)
        if not contracts:
            return {"symbol": symbol, "action": "no_contracts"}

        long_sym, short_sym = contracts
        quote = get_spread_quote(long_sym, short_sym)
        if not quote:
            return {"symbol": symbol, "action": "no_quote"}

        net_debit, long_mid, short_mid = quote
        equity = get_portfolio_equity()
        budget = equity * RISK_PCT
        qty = floor(budget / (net_debit * 100))

        if qty < 1:
            logger.warning("Budget %.2f too small for spread debit %.2f", budget, net_debit * 100)
            return {"symbol": symbol, "action": "insufficient_budget", "budget": budget, "cost": net_debit * 100}

        logger.info(
            "Opening bull call spread on %s: %s/%s qty=%d debit=%.2f (budget=%.2f)",
            symbol, long_sym, short_sym, qty, net_debit, budget,
        )

        order = open_bull_call_spread(long_sym, short_sym, qty, net_debit)

        print(json.dumps({
            "event": "trade_opened",
            "mode": "paper" if ALPACA_PAPER else "live",
            "symbol": symbol,
            "long": long_sym,
            "short": short_sym,
            "qty": qty,
            "net_debit": net_debit,
            "spread_debit_total": round(net_debit * qty * 100, 2),
            "equity": equity,
            "order_id": str(order.id),
        }))

        return {
            "symbol": symbol,
            "action": "open_spread",
            "order_id": str(order.id),
            "long": long_sym,
            "short": short_sym,
            "qty": qty,
            "net_debit": net_debit,
        }

    # SELL — close the bull call spread
    if rsi > 70 and c > ema9:
        positions = get_option_positions(symbol)
        if not positions:
            return {"symbol": symbol, "action": "nothing_to_close"}

        # Identify long leg (positive qty) and short leg (negative qty)
        long_pos = [p for p in positions if float(p.qty) > 0]
        short_pos = [p for p in positions if float(p.qty) < 0]

        if not long_pos or not short_pos:
            logger.warning("Unexpected option positions for %s: %s", symbol, positions)
            return {"symbol": symbol, "action": "unexpected_positions"}

        long_sym = long_pos[0].symbol
        short_sym = short_pos[0].symbol
        qty = int(abs(float(long_pos[0].qty)))

        quote = get_spread_quote(long_sym, short_sym)
        if quote:
            net_debit, long_mid, short_mid = quote
            # Alpaca MLEG convention: credits are negative, debits positive
            # Closing a spread = receiving credit, so negate the value
            limit_credit = -round(long_mid - short_mid, 2)
        else:
            # If we can't get quotes, use a small negative limit to ensure closure
            limit_credit = -0.01
            logger.warning("No quote for close; using min credit %.2f", limit_credit)

        # Calculate P&L from positions
        total_pnl = sum(float(p.unrealized_pl) for p in positions)

        logger.info(
            "Closing bull call spread on %s: %s/%s qty=%d credit=%.2f pnl=%.2f",
            symbol, long_sym, short_sym, qty, limit_credit, total_pnl,
        )

        order = close_bull_call_spread(long_sym, short_sym, qty, limit_credit)

        print(json.dumps({
            "event": "trade_closed",
            "mode": "paper" if ALPACA_PAPER else "live",
            "symbol": symbol,
            "long": long_sym,
            "short": short_sym,
            "qty": qty,
            "limit_credit": limit_credit,
            "spread_credit_total": round(limit_credit * qty * 100, 2),
            "pnl": round(total_pnl, 2),
            "order_id": str(order.id),
        }))

        return {
            "symbol": symbol,
            "action": "close_spread",
            "order_id": str(order.id),
            "long": long_sym,
            "short": short_sym,
            "qty": qty,
            "pnl": total_pnl,
        }

    # No trade
    return {
        "symbol": symbol,
        "action": "no_signal",
        "rsi": rsi,
        "close": c,
        "ema9": ema9,
    }


def lambda_handler(event, context):
    logger.info("Handling Scheduled Event (Trading Logic)")

    clock = trading_client.get_clock()
    if not clock.is_open:
        print(json.dumps({"event": "market_closed", "next_open": str(clock.next_open)}))
        return {"statusCode": 200, "body": "market_closed"}

    equity = get_portfolio_equity()
    print(json.dumps({
        "event": "run_started",
        "mode": "paper" if ALPACA_PAPER else "live",
        "equity": equity,
        "symbols": SYMBOLS,
    }))

    results = []
    for sym in SYMBOLS:
        try:
            result = evaluate_and_trade(sym.strip().upper())
            logger.info("%s -> %s", sym, result)
            results.append(result)
        except Exception as e:
            logger.exception("Error on %s", sym)
            results.append({"symbol": sym, "action": "error", "error": str(e)})

    return {"statusCode": 200, "body": results}


if __name__ == "__main__":
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
