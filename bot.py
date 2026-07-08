"""
bot.py — Main Trading Bot Entry Point
Runs the SMC + SuperTrend strategy on dYdX BTC-USD 24/7.
All settings are read live from config.cfg so Telegram changes
take effect on the next poll cycle without a restart.

Strategy:
  - Entry: BOS sequence → CHOCH → SuperTrend confirmation
  - Exit: Opposite CHOCH signal (structure-based) or emergency SL

Deployment: Render.com (free tier)
  - A Flask keep-alive server runs on PORT (default 8080) in a background thread.
  - UptimeRobot pings /health every 10 minutes so Render never sleeps.

Usage:
    python bot.py            # live trading
    python bot.py --dry-run  # simulate (no real orders, tracks P&L %)
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
    calculate_sl,
    init_dry_run_equity,
    is_daily_loss_limit_hit,
    record_dry_run_entry,
    record_dry_run_exit,
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


def _write_open_trade(side, entry, sl, size, signal_info=""):
    OPEN_TRADE_FILE.write_text(json.dumps(
        {"side": side, "entry": entry, "sl": sl, "size": size, "signal_info": signal_info}
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
      3. Run SMC + SuperTrend signal generation
      4. Handle exit signals (CHOCH reversal closes position)
      5. Handle entry signals (new CHOCH + SuperTrend → enter)
      6. Risk checks (daily loss limit, existing position)
    """
    logger.info("=" * 60)
    logger.info("  dYdX SMC+SuperTrend Trading Bot — STARTED")
    logger.info(f"  Network  : {cfg.network}")
    logger.info(f"  Symbol   : {cfg.symbol}")
    logger.info(f"  TF       : {cfg.candle_resolution}")
    logger.info(f"  Interval : {cfg.poll_interval}s")
    logger.info(f"  Dry Run  : {cfg.dry_run}")
    logger.info(f"  Strategy : BOS({cfg.min_bos_count}) → CHOCH → SuperTrend({cfg.supertrend_atr_period}/{cfg.supertrend_multiplier})")
    logger.info("=" * 60)

    await send_alert(
        f"🤖 <b>dYdX Bot STARTED</b>\n"
        f"Network: <code>{cfg.network.upper()}</code> | Symbol: <code>{cfg.symbol}</code>\n"
        f"Strategy: <code>SMC + SuperTrend</code>\n"
        f"Dry Run: <code>{cfg.dry_run}</code>\n\n"
        f"Send /start to open the control panel."
    )

    # Initialize dry-run equity if needed
    if cfg.dry_run:
        init_dry_run_equity(cfg.dry_run_equity)

    consecutive_errors = 0
    MAX_ERRORS = 10

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

            # ── 2. SMC + SuperTrend signal ───────────────────
            result = generate_signal(
                df,
                swing_length=cfg.swing_length,
                min_bos_count=cfg.min_bos_count,
                supertrend_atr_period=cfg.supertrend_atr_period,
                supertrend_multiplier=cfg.supertrend_multiplier,
            )

            latest_close = df["close"].iloc[-1]
            signal = result.signal
            label = {1: "BUY 🟢", -1: "SELL 🔴", 0: "HOLD ⚪"}[signal]
            trend_icon = "🟢" if result.smc_trend == 1 else ("🔴" if result.smc_trend == -1 else "⚪")
            st_icon = "🟢" if result.supertrend_dir == 1 else "🔴"

            logger.info(
                f"Price: ${latest_close:,.2f} | Signal: {label} | "
                f"SMC: {trend_icon} BOS#{result.bos_count} | ST: {st_icon} | "
                f"Event: {result.last_event}"
            )

            # ── 3. Handle exit signals (CHOCH reversal) ──────
            current_position = await client.get_position()

            if result.exit_signal and current_position is not None:
                pos_side = current_position.get("side")
                should_exit = False

                if result.smc_trend == 1 and pos_side == "SHORT":
                    should_exit = True
                elif result.smc_trend == -1 and pos_side == "LONG":
                    should_exit = True

                if should_exit:
                    logger.info(
                        f"📤 CHOCH exit signal — closing {pos_side} position | "
                        f"Reason: {result.exit_reason}"
                    )

                    # Calculate P&L before closing
                    entry_price = float(current_position.get("entryPrice", 0))
                    pos_size = abs(float(current_position.get("size", 0)))

                    close_result = await client.close_position()
                    if close_result:
                        pnl = (latest_close - entry_price) * pos_size * \
                              (1 if pos_side == "LONG" else -1)
                        pnl_pct = (pnl / entry_price / pos_size) * 100 if entry_price > 0 else 0

                        # Get equity for percentage tracking
                        try:
                            acc = await client.get_account()
                            equity = float(acc.get("equity", 0))
                        except Exception:
                            equity = 0

                        record_trade_pnl(pnl, equity)
                        _clear_open_trade()

                        dry = close_result.get("status") == "DRY_RUN"
                        await send_alert(
                            f"{'🔵 [DRY RUN] ' if dry else ''}📤 <b>CHOCH Exit</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"Closed : <code>{pos_side}</code>\n"
                            f"Entry  : <code>${entry_price:,.2f}</code>\n"
                            f"Exit   : <code>${latest_close:,.2f}</code>\n"
                            f"PnL    : <code>${pnl:+.2f} ({pnl_pct:+.2f}%)</code>\n"
                            f"Reason : <code>{result.exit_reason}</code>"
                        )

                    await asyncio.sleep(2)
                    current_position = None  # position closed

            # ── Handle dry-run exit ──────────────────────────
            if cfg.dry_run and result.exit_signal:
                dry_exit_result = record_dry_run_exit(latest_close)
                if dry_exit_result:
                    await send_alert(
                        f"🔵 [DRY RUN] 📤 <b>CHOCH Exit</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Side   : <code>{dry_exit_result['side']}</code>\n"
                        f"Entry  : <code>${dry_exit_result['entry']:,.2f}</code>\n"
                        f"Exit   : <code>${dry_exit_result['exit']:,.2f}</code>\n"
                        f"PnL    : <code>${dry_exit_result['pnl_usdc']:+.2f} ({dry_exit_result['pnl_pct']:+.2f}%)</code>"
                    )

            # ── 4. Risk checks ────────────────────────────────
            if is_daily_loss_limit_hit():
                logger.warning("Daily loss limit hit — skipping cycle.")
                await asyncio.sleep(cfg.poll_interval)
                continue

            if not should_enter(signal, current_position):
                await asyncio.sleep(cfg.poll_interval)
                consecutive_errors = 0
                continue

            # Alert on new signal
            if signal != 0:
                confluence_str = ", ".join(result.confluence) if result.confluence else "none"
                await send_alert(
                    f"📡 <b>New Signal: {label}</b>\n"
                    f"Price: <code>${latest_close:,.2f}</code>\n"
                    f"Event: <code>{result.last_event}</code>\n"
                    f"Confluence: <code>{confluence_str}</code>\n"
                    f"Network: <code>{cfg.network.upper()}</code> | TF: <code>{cfg.candle_resolution}</code>"
                )

            # ── 5. Flip: close existing if opposite ──────────
            if current_position is not None:
                pos_side = current_position.get("side")
                if (signal == 1 and pos_side == "SHORT") or \
                   (signal == -1 and pos_side == "LONG"):
                    logger.info(f"Flipping {pos_side} → closing first...")
                    await client.close_position()
                    await asyncio.sleep(2)

            # ── 6. Place order ────────────────────────────────
            # Fetch live equity so position size scales with the account
            try:
                acc         = await client.get_account()
                equity_usdc = float(acc.get("equity", 0))
                if equity_usdc <= 0:
                    raise ValueError("Account equity is zero or unavailable.")
            except Exception as e:
                if cfg.dry_run:
                    equity_usdc = cfg.dry_run_equity
                else:
                    logger.error(f"Could not fetch equity for position sizing: {e} — skipping cycle.")
                    await asyncio.sleep(cfg.poll_interval)
                    continue

            order_side = "BUY" if signal == 1 else "SELL"
            size_btc   = calculate_position_size(latest_close, equity_usdc)

            result_order = await client.place_market_order(
                side=order_side,
                size=size_btc,
            )

            if result_order:
                entry_price = latest_close
                sl_price = calculate_sl(entry_price, order_side, df)

                logger.info(
                    f"✅ Order | Entry: ${entry_price:,.2f} | "
                    f"SL: ${sl_price:,.2f} | Exit: CHOCH-based"
                )
                _write_open_trade(
                    order_side, entry_price, sl_price, size_btc,
                    signal_info=result.last_event,
                )

                # Track dry-run entry
                if cfg.dry_run:
                    record_dry_run_entry(order_side, entry_price, size_btc)

                collateral = equity_usdc * cfg.position_size_pct
                dry = result_order.get("status") == "DRY_RUN"
                await send_alert(
                    f"{'🔵 [DRY RUN] ' if dry else ''}✅ <b>Order Placed</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Side        : <code>{order_side}</code>\n"
                    f"Size        : <code>{size_btc} BTC</code>\n"
                    f"Collateral  : <code>${collateral:.2f} USDC ({cfg.position_size_pct*100:.0f}% of equity)</code>\n"
                    f"Leverage    : <code>{cfg.leverage}x</code>\n"
                    f"Entry       : <code>${entry_price:,.2f}</code>\n"
                    f"SL          : <code>${sl_price:,.2f}</code>\n"
                    f"Exit        : <code>CHOCH-based (structure reversal)</code>\n"
                    f"Signal      : <code>{result.last_event}</code>"
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
# SL-Only Monitor (emergency stop-loss)
# -----------------------------------------------------------

async def sl_monitor(client: DydxClient):
    """
    Checks every 5 seconds whether SL was hit.
    Sends a Telegram alert and closes the position when triggered.
    TP is no longer monitored — exits are driven by CHOCH signals.
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

            if hit_sl:
                logger.info(f"🛑 Stop Loss hit at ${price:,.2f}")
                await client.close_position()
                pnl = (price - trade["entry"]) * trade["size"] * \
                      (1 if trade["side"] == "BUY" else -1)
                pnl_pct = (pnl / trade["entry"] / trade["size"]) * 100 if trade["entry"] > 0 else 0

                # Get equity for recording
                try:
                    acc = await client.get_account()
                    equity = float(acc.get("equity", 0))
                except Exception:
                    equity = 0

                record_trade_pnl(pnl, equity)
                _clear_open_trade()

                # Handle dry-run SL exit
                if cfg.dry_run:
                    record_dry_run_exit(price)

                await send_alert(
                    f"🛑 <b>Stop Loss hit!</b>\n"
                    f"Price: <code>${price:,.2f}</code> | PnL: <code>${pnl:+.2f} ({pnl_pct:+.2f}%)</code>"
                )

        except Exception as e:
            logger.error(f"SL monitor error: {e}", exc_info=False)

        await asyncio.sleep(5)


# -----------------------------------------------------------
# Entry Point
# -----------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="dYdX SMC+SuperTrend Trading Bot")
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
            sl_monitor(client),
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
        "service": "dYdX SMC+SuperTrend Trading Bot",
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
        "service": "dYdX SMC+SuperTrend Trading Bot",
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
