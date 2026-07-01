"""
bot.py — Main Trading Bot Entry Point
Runs the SMC strategy on dYdX BTC-USD 24/7.

Usage:
    python bot.py            # live trading
    python bot.py --dry-run  # simulate (no real orders)
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import colorlog
from dotenv import load_dotenv

from dydx_client import DydxClient
from logic import generate_signal
from risk import (
    calculate_position_size,
    calculate_sl_tp,
    is_daily_loss_limit_hit,
    record_trade_pnl,
    should_enter,
)

load_dotenv()

# -----------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------

def setup_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Colored console handler
    console_handler = colorlog.StreamHandler()
    console_handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    ))

    # Rotating file handler
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        "logs/bot.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)


logger = logging.getLogger("bot")

# -----------------------------------------------------------
# Constants from .env
# -----------------------------------------------------------
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", 60))
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", 100))
CANDLE_RESOLUTION = os.getenv("CANDLE_RESOLUTION", "15MINS")
SYMBOL = os.getenv("TRADE_SYMBOL", "BTC-USD")

# -----------------------------------------------------------
# Main Trading Loop
# -----------------------------------------------------------

async def trading_loop(client: DydxClient):
    """
    Infinite loop that:
    1. Fetches latest candles
    2. Runs SMC indicator → generates signal
    3. Applies risk checks
    4. Places / closes orders
    5. Waits POLL_INTERVAL seconds
    """
    logger.info("=" * 60)
    logger.info("  dYdX SMC Trading Bot — STARTED")
    logger.info(f"  Symbol   : {SYMBOL}")
    logger.info(f"  TF       : {CANDLE_RESOLUTION}")
    logger.info(f"  Interval : {POLL_INTERVAL}s")
    logger.info(f"  Dry Run  : {client.dry_run}")
    logger.info("=" * 60)

    consecutive_errors = 0
    max_errors = 10

    while True:
        try:
            # ------------------------------------------------
            # 1. Fetch candle data
            # ------------------------------------------------
            df = await client.get_candles(
                symbol=SYMBOL,
                resolution=CANDLE_RESOLUTION,
                limit=CANDLE_LIMIT,
            )

            # ------------------------------------------------
            # 2. Generate SMC signal
            # ------------------------------------------------
            signal = generate_signal(df)
            latest_close = df["close"].iloc[-1]

            signal_label = {1: "BUY 🟢", -1: "SELL 🔴", 0: "HOLD ⚪"}[signal]
            logger.info(f"Price: ${latest_close:,.2f} | Signal: {signal_label}")

            # ------------------------------------------------
            # 3. Risk checks
            # ------------------------------------------------
            if is_daily_loss_limit_hit():
                logger.warning("Daily loss limit hit — skipping this cycle.")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            current_position = await client.get_position()

            if not should_enter(signal, current_position):
                await asyncio.sleep(POLL_INTERVAL)
                consecutive_errors = 0
                continue

            # ------------------------------------------------
            # 4. Close existing position if flipping sides
            # ------------------------------------------------
            if current_position is not None:
                pos_side = current_position.get("side")
                if (signal == 1 and pos_side == "SHORT") or \
                   (signal == -1 and pos_side == "LONG"):
                    logger.info(f"Flipping from {pos_side} — closing position first.")
                    await client.close_position()
                    await asyncio.sleep(2)  # brief pause after close

            # ------------------------------------------------
            # 5. Calculate size & place order
            # ------------------------------------------------
            size_btc = calculate_position_size(latest_close)
            order_side = "BUY" if signal == 1 else "SELL"

            result = await client.place_market_order(
                side=order_side,
                size=size_btc,
            )

            if result:
                entry_price = latest_close  # approximate; real fill comes via events
                sl_price, tp_price = calculate_sl_tp(entry_price, order_side, df)

                logger.info(
                    f"✅ Order filled (approx)  |  Entry: ${entry_price:,.2f}  |"
                    f"  SL: ${sl_price:,.2f}  |  TP: ${tp_price:,.2f}"
                )

                # Store open trade info in state for SL/TP monitoring
                _write_open_trade(order_side, entry_price, sl_price, tp_price, size_btc)

            consecutive_errors = 0

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Shutting down...")
            break

        except Exception as e:
            consecutive_errors += 1
            wait_time = min(60 * consecutive_errors, 600)  # exponential backoff, max 10 min
            logger.error(
                f"Error in trading loop ({consecutive_errors}/{max_errors}): {e}",
                exc_info=True,
            )

            if consecutive_errors >= max_errors:
                logger.critical("Too many consecutive errors. Stopping bot.")
                raise

            logger.info(f"Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
            continue

        await asyncio.sleep(POLL_INTERVAL)


# -----------------------------------------------------------
# SL/TP Monitor (runs alongside main loop)
# -----------------------------------------------------------

import json

OPEN_TRADE_FILE = Path("open_trade.json")


def _write_open_trade(side, entry, sl, tp, size):
    OPEN_TRADE_FILE.write_text(json.dumps({
        "side": side, "entry": entry,
        "sl": sl, "tp": tp, "size": size
    }))


def _clear_open_trade():
    if OPEN_TRADE_FILE.exists():
        OPEN_TRADE_FILE.unlink()


async def sl_tp_monitor(client: DydxClient):
    """
    Secondary loop that watches the current price and
    closes the position if SL or TP is hit.
    Checks every 5 seconds.
    """
    while True:
        try:
            if not OPEN_TRADE_FILE.exists():
                await asyncio.sleep(5)
                continue

            trade = json.loads(OPEN_TRADE_FILE.read_text())
            ob = await client.get_orderbook()
            price = ob.get("bid") if trade["side"] == "BUY" else ob.get("ask")

            if price is None:
                await asyncio.sleep(5)
                continue

            hit_sl = (trade["side"] == "BUY" and price <= trade["sl"]) or \
                     (trade["side"] == "SELL" and price >= trade["sl"])
            hit_tp = (trade["side"] == "BUY" and price >= trade["tp"]) or \
                     (trade["side"] == "SELL" and price <= trade["tp"])

            if hit_sl:
                logger.warning(f"🛑 Stop Loss hit at ${price:,.2f} (SL: ${trade['sl']:,.2f})")
                await client.close_position()
                pnl = (price - trade["entry"]) * trade["size"] * (1 if trade["side"] == "BUY" else -1)
                record_trade_pnl(pnl)
                _clear_open_trade()

            elif hit_tp:
                logger.info(f"🎯 Take Profit hit at ${price:,.2f} (TP: ${trade['tp']:,.2f})")
                await client.close_position()
                pnl = (price - trade["entry"]) * trade["size"] * (1 if trade["side"] == "BUY" else -1)
                record_trade_pnl(pnl)
                _clear_open_trade()

        except Exception as e:
            logger.error(f"SL/TP monitor error: {e}", exc_info=False)

        await asyncio.sleep(5)


# -----------------------------------------------------------
# Entry Point
# -----------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="dYdX SMC Trading Bot")
    parser.add_argument("--dry-run", action="store_true", help="Simulate trades without placing real orders")
    args = parser.parse_args()

    # Override DRY_RUN via CLI flag
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    setup_logging()

    client = DydxClient()

    try:
        await client.connect()

        # Run main loop + SL/TP monitor concurrently
        await asyncio.gather(
            trading_loop(client),
            sl_tp_monitor(client),
        )

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
