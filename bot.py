"""
bot.py — dYdX SuperTrend Trading Bot

Pure SuperTrend flip trading:
  • SuperTrend says BUY  → BUY
  • SuperTrend says SELL → SELL
  • 3-second signal confirmation (re-check after 3s before acting)
  • Skip trades when ATR is too small (not worth the fees)
  • Emergency percentage SL monitor

No partial TP. No trailing stops. No cooldowns. No regime filters.
Just the indicator.

Deployment: Render.com
  Flask keep-alive on PORT (default 8080) for UptimeRobot.
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
# Logging
# -----------------------------------------------------------

def setup_logging():
    log_level = cfg.log_level.upper()
    Path("logs").mkdir(exist_ok=True)

    console = colorlog.StreamHandler()
    console.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG": "cyan", "INFO": "green",
            "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold_red",
        },
    ))

    fh = RotatingFileHandler("logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console)
    root.addHandler(fh)


logger = logging.getLogger("bot")

# -----------------------------------------------------------
# Open Trade File
# -----------------------------------------------------------
OPEN_TRADE_FILE = Path("open_trade.json")


def _write_trade(side, entry, sl, size):
    OPEN_TRADE_FILE.write_text(json.dumps(
        {"side": side, "entry": entry, "sl": sl, "size": size}
    ))


def _read_trade() -> dict:
    if OPEN_TRADE_FILE.exists():
        try:
            return json.loads(OPEN_TRADE_FILE.read_text())
        except Exception:
            pass
    return {}


def _clear_trade():
    if OPEN_TRADE_FILE.exists():
        OPEN_TRADE_FILE.unlink()


# -----------------------------------------------------------
# Signal Confirmation (3-second re-check)
# -----------------------------------------------------------

CONFIRM_DELAY = 3  # seconds


async def _confirm_signal(client: DydxClient, first_signal: int) -> bool:
    """
    Wait 3 seconds, re-fetch candles, re-run SuperTrend.
    Signal must persist → confirmed.
    """
    logger.info(f"⏳ Confirming signal ({CONFIRM_DELAY}s)...")
    await asyncio.sleep(CONFIRM_DELAY)

    df = await client.get_candles(
        symbol=cfg.symbol,
        resolution=cfg.candle_resolution,
        limit=cfg.candle_limit,
    )

    result = generate_signal(
        df,
        atr_period=cfg.st_atr_period,
        multiplier=cfg.st_multiplier,
    )

    if result.signal == first_signal:
        logger.info("✅ Signal confirmed!")
        return True
    else:
        logger.info(f"❌ Signal NOT confirmed (was {first_signal}, now {result.signal})")
        return False


# -----------------------------------------------------------
# Main Trading Loop
# -----------------------------------------------------------

async def trading_loop(client: DydxClient):
    """
    Simple loop:
      1. Fetch candles → run SuperTrend
      2. If flip detected → wait 3s → re-check → confirmed?
      3. If in position and opposite flip → close + open opposite
      4. If no position and flip → open new position
      5. Daily loss guard
    """
    logger.info("=" * 50)
    logger.info("  dYdX SuperTrend Bot — STARTED")
    logger.info(f"  Network : {cfg.network}")
    logger.info(f"  Symbol  : {cfg.symbol}")
    logger.info(f"  TF      : {cfg.candle_resolution}")
    logger.info(f"  ATR     : {cfg.st_atr_period} | Mult: {cfg.st_multiplier}")
    logger.info(f"  Dry Run : {cfg.dry_run}")
    logger.info("=" * 50)

    await send_alert(
        f"⚡ <b>SuperTrend Bot STARTED</b>\n"
        f"Network: <code>{cfg.network.upper()}</code>\n"
        f"Symbol: <code>{cfg.symbol}</code> | TF: <code>{cfg.candle_resolution}</code>\n"
        f"SuperTrend: <code>ATR={cfg.st_atr_period}, ×{cfg.st_multiplier}</code>\n"
        f"Dry Run: <code>{cfg.dry_run}</code>\n\n"
        f"Send /start to open the control panel."
    )

    if cfg.dry_run:
        init_dry_run_equity(cfg.dry_run_equity)

    consecutive_errors = 0

    while True:
        if cfg.paused:
            await asyncio.sleep(cfg.poll_interval)
            continue

        try:
            # ── 1. Fetch candles & run SuperTrend ──
            df = await client.get_candles(
                symbol=cfg.symbol,
                resolution=cfg.candle_resolution,
                limit=cfg.candle_limit,
            )

            result = generate_signal(
                df,
                atr_period=cfg.st_atr_period,
                multiplier=cfg.st_multiplier,
            )

            signal = result.signal
            price = result.price
            label = {1: "BUY 🟢", -1: "SELL 🔴", 0: "HOLD ⚪"}[signal]

            logger.info(
                f"Price: ${price:,.2f} | {label} | "
                f"ATR: {result.atr:.2f} | Band: ${result.st_band:,.2f}"
            )

            # ── 2. Daily loss check ──
            if is_daily_loss_limit_hit():
                await asyncio.sleep(cfg.poll_interval)
                continue

            # ── 3. Handle existing position — opposite flip = exit ──
            current_position = await client.get_position()
            open_trade = _read_trade()

            if current_position and open_trade and signal != 0:
                pos_side = current_position.get("side")
                need_close = (
                    (pos_side == "LONG" and signal == -1) or
                    (pos_side == "SHORT" and signal == 1)
                )

                if need_close:
                    # Confirm the flip before closing
                    confirmed = await _confirm_signal(client, signal)
                    if not confirmed:
                        await asyncio.sleep(cfg.poll_interval)
                        continue

                    entry_price = float(current_position.get("entryPrice", open_trade.get("entry", 0)))
                    pos_size = abs(float(current_position.get("size", 0)))

                    logger.info(f"📤 Closing {pos_side} — SuperTrend flipped")
                    close_result = await client.close_position()

                    if close_result:
                        pnl = (price - entry_price) * pos_size * (1 if pos_side == "LONG" else -1)
                        pnl_pct = (pnl / (entry_price * pos_size)) * 100 if entry_price > 0 else 0

                        try:
                            acc = await client.get_account()
                            equity = float(acc.get("equity", 0))
                        except Exception:
                            equity = 0

                        record_trade_pnl(pnl, equity)
                        _clear_trade()

                        if cfg.dry_run:
                            record_dry_run_exit(price)

                        dry = close_result.get("status") == "DRY_RUN"
                        await send_alert(
                            f"{'🔵 [DRY] ' if dry else ''}📤 <b>Closed {pos_side}</b>\n"
                            f"Entry: <code>${entry_price:,.2f}</code>\n"
                            f"Exit : <code>${price:,.2f}</code>\n"
                            f"PnL  : <code>${pnl:+.2f} ({pnl_pct:+.2f}%)</code>"
                        )

                    await asyncio.sleep(1)
                    current_position = None
                    # Fall through to open opposite direction below

            # ── Handle dry-run exit (no real position to check) ──
            if cfg.dry_run and open_trade and signal != 0:
                dry_side = open_trade.get("side", "")
                need_dry_close = (
                    (dry_side == "BUY" and signal == -1) or
                    (dry_side == "SELL" and signal == 1)
                )
                if need_dry_close and not current_position:
                    dry_exit = record_dry_run_exit(price)
                    if dry_exit:
                        _clear_trade()
                        await send_alert(
                            f"🔵 [DRY] 📤 <b>Exit</b>\n"
                            f"Side : <code>{dry_exit['side']}</code>\n"
                            f"Entry: <code>${dry_exit['entry']:,.2f}</code>\n"
                            f"Exit : <code>${dry_exit['exit']:,.2f}</code>\n"
                            f"PnL  : <code>${dry_exit['pnl_usdc']:+.2f} ({dry_exit['pnl_pct']:+.2f}%)</code>"
                        )

            # ── 4. Should we enter? ──
            if not should_enter(signal, current_position):
                await asyncio.sleep(cfg.poll_interval)
                consecutive_errors = 0
                continue

            # ── 5. Confirm signal (3-second re-check) ──
            confirmed = await _confirm_signal(client, signal)
            if not confirmed:
                await asyncio.sleep(cfg.poll_interval)
                continue

            # ── 6. Open new position ──
            # Close opposite position first if it still exists
            if current_position:
                pos_side = current_position.get("side")
                if (signal == 1 and pos_side == "SHORT") or (signal == -1 and pos_side == "LONG"):
                    await client.close_position()
                    _clear_trade()
                    await asyncio.sleep(1)

            # Get equity
            try:
                acc = await client.get_account()
                equity_usdc = float(acc.get("equity", 0))
                if equity_usdc <= 0:
                    raise ValueError("Zero equity")
            except Exception as e:
                if cfg.dry_run:
                    equity_usdc = cfg.dry_run_equity
                else:
                    logger.error(f"Equity fetch failed: {e}")
                    await asyncio.sleep(cfg.poll_interval)
                    continue

            order_side = "BUY" if signal == 1 else "SELL"
            size_btc = calculate_position_size(price, equity_usdc)

            order_result = await client.place_market_order(side=order_side, size=size_btc)

            if order_result:
                sl_price = calculate_sl(price, order_side)

                _write_trade(order_side, price, sl_price, size_btc)

                if cfg.dry_run:
                    record_dry_run_entry(order_side, price, size_btc)

                dry = order_result.get("status") == "DRY_RUN"
                collateral = equity_usdc * cfg.position_size_pct
                await send_alert(
                    f"{'🔵 [DRY] ' if dry else ''}✅ <b>{order_side}</b>\n"
                    f"Size : <code>{size_btc} BTC</code>\n"
                    f"Entry: <code>${price:,.2f}</code>\n"
                    f"SL   : <code>${sl_price:,.2f}</code>\n"
                    f"Cost : <code>${collateral:.2f} ({cfg.position_size_pct*100:.0f}% × {cfg.leverage}x)</code>"
                )

            consecutive_errors = 0

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break

        except Exception as e:
            consecutive_errors += 1
            wait = min(30 * consecutive_errors, 600)
            logger.error(f"Error ({consecutive_errors}): {e}", exc_info=True)
            if consecutive_errors >= 10:
                await send_alert(f"🚨 <b>Bot stopped after 10 errors!</b>\n<code>{e}</code>")
                raise
            await asyncio.sleep(wait)
            continue

        await asyncio.sleep(cfg.poll_interval)


# -----------------------------------------------------------
# Emergency SL Monitor
# -----------------------------------------------------------

async def sl_monitor(client: DydxClient):
    """Check every 5s if emergency SL is hit."""
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

            sl = trade.get("sl", 0)
            hit = (trade["side"] == "BUY" and price <= sl) or \
                  (trade["side"] == "SELL" and price >= sl)

            if hit:
                logger.info(f"🛑 Emergency SL hit! Price: ${price:,.2f} | SL: ${sl:,.2f}")
                await client.close_position()

                pnl = (price - trade["entry"]) * trade["size"] * \
                      (1 if trade["side"] == "BUY" else -1)

                try:
                    acc = await client.get_account()
                    equity = float(acc.get("equity", 0))
                except Exception:
                    equity = 0

                record_trade_pnl(pnl, equity)
                _clear_trade()

                if cfg.dry_run:
                    record_dry_run_exit(price)

                await send_alert(
                    f"🛑 <b>Emergency SL Hit!</b>\n"
                    f"Price: <code>${price:,.2f}</code>\n"
                    f"SL: <code>${sl:,.2f}</code>\n"
                    f"PnL: <code>${pnl:+.2f}</code>"
                )

        except Exception as e:
            logger.error(f"SL monitor error: {e}", exc_info=False)

        await asyncio.sleep(5)


# -----------------------------------------------------------
# Entry Point
# -----------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="dYdX SuperTrend Bot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        cfg.set("dry_run", True)

    setup_logging()

    client = DydxClient()

    try:
        await client.connect()
        await asyncio.gather(
            trading_loop(client),
            sl_monitor(client),
            start_telegram_bot(client),
        )
    finally:
        await client.close()


# -----------------------------------------------------------
# Keep-Alive (Render + UptimeRobot)
# -----------------------------------------------------------

_flask_app = Flask(__name__)
_start_time = None


@_flask_app.route("/")
def index():
    return jsonify({"status": "running", "service": "dYdX SuperTrend Bot"})


@_flask_app.route("/health")
def health():
    import time
    uptime = int(time.time() - _start_time) if _start_time else 0
    h, r = divmod(uptime, 3600)
    m, s = divmod(r, 60)
    return jsonify({
        "status": "ok",
        "uptime": f"{h}h {m}m {s}s",
        "network": os.getenv("DYDX_NETWORK", "mainnet"),
        "dry_run": os.getenv("DRY_RUN", "true"),
    })


def _run_flask():
    port = int(os.getenv("PORT", 8080))
    _flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    import time
    _start_time = time.time()

    flask_thread = threading.Thread(target=_run_flask, daemon=True, name="keep-alive")
    flask_thread.start()
    logging.getLogger("bot").info(f"Keep-alive on port {os.getenv('PORT', 8080)}")

    asyncio.run(main())
