"""
bot.py — Main Trading Bot Entry Point
Runs the SMC strategy on dYdX BTC-USD 24/7.
All settings are read live from config.cfg so Telegram changes
take effect on the next poll cycle without a restart.

Deployment: Render.com (free tier)
  - A Flask keep-alive server runs on PORT (default 8080) in a background thread.
  - UptimeRobot pings /health every 10 minutes so Render never sleeps.

Usage:
    python bot.py            # live trading
    python bot.py --dry-run  # simulate (no real orders)
"""

import argparse
import asyncio
import json
import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify

import colorlog
from dotenv import load_dotenv

from config import cfg
from dydx_client import DydxClient
from logic import generate_signal
from risk import (
    calculate_position_size,
    calculate_sl_tp,
    is_daily_loss_limit_hit,
    record_trade_pnl,
    should_enter,
)
from telegram_bot import send_alert, start_telegram_bot

load_dotenv()

# -----------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------

def setup_logging():
    log_level = cfg.log_level.upper()
    Path("logs").mkdir(exist_ok=True)

    console_handler = colorlog.StreamHandler()
    console_handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))

    file_handler = RotatingFileHandler(
        "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=5
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
# Trade State Files
# -----------------------------------------------------------
OPEN_TRADE_FILE = Path("open_trade.json")


def _write_open_trade(side, entry, sl, tp, size):
    OPEN_TRADE_FILE.write_text(json.dumps(
        {"side": side, "entry": entry, "sl": sl, "tp": tp, "size": size}
    ))


def _clear_open_trade():
    if OPEN_TRADE_FILE.exists():
        OPEN_TRADE_FILE.unlink()


# -----------------------------------------------------------
# Main Trading Loop
# -----------------------------------------------------------

async def trading_loop(client: DydxClient):
    """
    Infinite poll loop:
      1. Check pause flag (set by Telegram /pause)
      2. Fetch candles using live cfg values
      3. Run SMC signal generation
      4. Risk checks (daily loss limit, existing position)
      5. Place order with live leverage/size from cfg
    """
    logger.info("=" * 60)
    logger.info("  dYdX SMC Trading Bot — STARTED")
    logger.info(f"  Network  : {cfg.network}")
    logger.info(f"  Symbol   : {cfg.symbol}")
    logger.info(f"  TF       : {cfg.candle_resolution}")
    logger.info(f"  Interval : {cfg.poll_interval}s")
    logger.info(f"  Dry Run  : {cfg.dry_run}")
    logger.info("=" * 60)

    await send_alert(
        f"🤖 <b>dYdX Bot STARTED</b>\n"
        f"Network: <code>{cfg.network.upper()}</code> | Symbol: <code>{cfg.symbol}</code>\n"
        f"Dry Run: <code>{cfg.dry_run}</code>\n\n"
        f"Send /start to open the control panel."
    )

    consecutive_errors = 0
    MAX_ERRORS = 10
    last_signal = 0  # track signal changes to avoid re-alerting

    while True:
        # ── Check pause ──────────────────────────────────────
        if cfg.paused:
            logger.debug("Bot is paused. Sleeping...")
            await asyncio.sleep(cfg.poll_interval)
            continue

        try:
            # ── 1. Fetch candles ─────────────────────────────
            df = await client.get_candles(
                symbol=cfg.symbol,
                resolution=cfg.candle_resolution,
                limit=cfg.candle_limit,
            )

            # ── 2. SMC signal ─────────────────────────────────
            signal = generate_signal(df)
            latest_close = df["close"].iloc[-1]

            label = {1: "BUY 🟢", -1: "SELL 🔴", 0: "HOLD ⚪"}[signal]
            logger.info(
                f"Price: ${latest_close:,.2f} | Signal: {label} | "
                f"Leverage: {cfg.leverage}x | Size: ${cfg.position_size_usdc}"
            )

            # Alert on signal change
            if signal != 0 and signal != last_signal:
                await send_alert(
                    f"📡 <b>New Signal: {label}</b>\n"
                    f"Price: <code>${latest_close:,.2f}</code>\n"
                    f"Network: <code>{cfg.network.upper()}</code> | TF: <code>{cfg.candle_resolution}</code>"
                )
            last_signal = signal

            # ── 3. Risk checks ────────────────────────────────
            if is_daily_loss_limit_hit():
                logger.warning("Daily loss limit hit — skipping cycle.")
                await asyncio.sleep(cfg.poll_interval)
                continue

            current_position = await client.get_position()

            if not should_enter(signal, current_position):
                await asyncio.sleep(cfg.poll_interval)
                consecutive_errors = 0
                continue

            # ── 4. Flip: close existing if opposite ──────────
            if current_position is not None:
                pos_side = current_position.get("side")
                if (signal == 1 and pos_side == "SHORT") or \
                   (signal == -1 and pos_side == "LONG"):
                    logger.info(f"Flipping {pos_side} → closing first...")
                    await client.close_position()
                    await asyncio.sleep(2)

            # ── 5. Place order ────────────────────────────────
            size_btc   = calculate_position_size(latest_close)
            order_side = "BUY" if signal == 1 else "SELL"

            result = await client.place_market_order(
                side=order_side,
                size=size_btc,
            )

            if result:
                entry_price        = latest_close
                sl_price, tp_price = calculate_sl_tp(entry_price, order_side, df)

                logger.info(
                    f"✅ Order | Entry: ${entry_price:,.2f} | "
                    f"SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}"
                )
                _write_open_trade(order_side, entry_price, sl_price, tp_price, size_btc)

                dry = result.get("status") == "DRY_RUN"
                await send_alert(
                    f"{'🔵 [DRY RUN] ' if dry else ''}✅ <b>Order Placed</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Side  : <code>{order_side}</code>\n"
                    f"Size  : <code>{size_btc} BTC</code>\n"
                    f"Entry : <code>${entry_price:,.2f}</code>\n"
                    f"SL    : <code>${sl_price:,.2f}</code>  |  TP: <code>${tp_price:,.2f}</code>"
                )

            consecutive_errors = 0

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Shutting down...")
            break

        except Exception as e:
            consecutive_errors += 1
            wait_time = min(30 * consecutive_errors, 600)
            logger.error(
                f"Trading loop error ({consecutive_errors}/{MAX_ERRORS}): {e}",
                exc_info=True,
            )
            if consecutive_errors >= MAX_ERRORS:
                await send_alert(f"🚨 <b>CRITICAL: Bot stopped after {MAX_ERRORS} errors!</b>\n<code>{e}</code>")
                raise

            logger.info(f"Retry in {wait_time}s...")
            await asyncio.sleep(wait_time)
            continue

        await asyncio.sleep(cfg.poll_interval)


# -----------------------------------------------------------
# SL/TP Price Monitor
# -----------------------------------------------------------

async def sl_tp_monitor(client: DydxClient):
    """
    Checks every 5 seconds whether SL or TP was hit.
    Sends a Telegram alert and closes the position when triggered.
    """
    while True:
        try:
            if not OPEN_TRADE_FILE.exists():
                await asyncio.sleep(5)
                continue

            trade = json.loads(OPEN_TRADE_FILE.read_text())
            ob    = await client.get_orderbook()
            price = ob.get("bid") if trade["side"] == "BUY" else ob.get("ask")

            if price is None:
                await asyncio.sleep(5)
                continue

            hit_sl = (trade["side"] == "BUY"  and price <= trade["sl"]) or \
                     (trade["side"] == "SELL" and price >= trade["sl"])
            hit_tp = (trade["side"] == "BUY"  and price >= trade["tp"]) or \
                     (trade["side"] == "SELL" and price <= trade["tp"])

            if hit_sl or hit_tp:
                tag = "🛑 Stop Loss" if hit_sl else "🎯 Take Profit"
                logger.info(f"{tag} hit at ${price:,.2f}")
                await client.close_position()
                pnl = (price - trade["entry"]) * trade["size"] * \
                      (1 if trade["side"] == "BUY" else -1)
                record_trade_pnl(pnl)
                _clear_open_trade()
                await send_alert(
                    f"{tag} <b>hit!</b>\n"
                    f"Price: <code>${price:,.2f}</code> | PnL: <code>${pnl:+.2f} USDC</code>"
                )

        except Exception as e:
            logger.error(f"SL/TP monitor error: {e}", exc_info=False)

        await asyncio.sleep(5)


# -----------------------------------------------------------
# Entry Point
# -----------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="dYdX SMC Trading Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate trades without placing real orders")
    args = parser.parse_args()

    if args.dry_run:
        cfg.set("dry_run", True)

    setup_logging()

    client = DydxClient()

    try:
        await client.connect()

        # Run all three tasks concurrently in the same event loop
        await asyncio.gather(
            trading_loop(client),
            sl_tp_monitor(client),
            start_telegram_bot(client),
        )

    finally:
        await client.close()


# -----------------------------------------------------------
# Keep-Alive Web Server (for Render + UptimeRobot)
# -----------------------------------------------------------

_flask_app = Flask(__name__)
_start_time = None


@_flask_app.route("/")
def index():
    return jsonify({
        "status": "running",
        "service": "dYdX SMC Trading Bot",
        "message": "Bot is alive and trading. Visit /health for uptime info."
    })


@_flask_app.route("/health")
def health():
    import time
    uptime_seconds = int(time.time() - _start_time) if _start_time else 0
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return jsonify({
        "status": "ok",
        "uptime": f"{hours}h {minutes}m {seconds}s",
        "service": "dYdX SMC Trading Bot",
        "network": os.getenv("DYDX_NETWORK", "mainnet"),
        "dry_run": os.getenv("DRY_RUN", "true"),
    })


def _run_flask():
    """Run Flask in a daemon thread. Render needs an open HTTP port."""
    port = int(os.getenv("PORT", 8080))
    # Use werkzeug's simple server — no extra config needed on Render
    _flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    import time
    _start_time = time.time()

    # Start the keep-alive Flask server in a background daemon thread
    flask_thread = threading.Thread(target=_run_flask, daemon=True, name="keep-alive")
    flask_thread.start()
    logging.getLogger("bot").info(
        f"Keep-alive server started on port {os.getenv('PORT', 8080)} "
        "→ UptimeRobot should ping /health every 10 min"
    )

    asyncio.run(main())
