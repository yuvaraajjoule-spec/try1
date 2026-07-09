"""
bot.py — Main Trading Bot Entry Point
Runs the SuperTrend Sniper strategy on dYdX BTC-USD 24/7.
All settings are read live from config.cfg so Telegram changes
take effect on the next poll cycle without a restart.

Strategy: SuperTrend Sniper — Pure SuperTrend flip trading
  - Entry: Every SuperTrend trend flip = trade
  - Exit: SuperTrend band (natural trailing stop) / Opposite flip / Emergency SL
  - Fee-aware: Only enters when ATR > 2× round-trip fee cost

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
    get_trailing_sl,
    increment_hold_counter,
    init_dry_run_equity,
    is_cooldown_active,
    is_daily_loss_limit_hit,
    record_dry_run_entry,
    record_dry_run_exit,
    record_trade_pnl,
    reset_hold_counter,
    save_trailing_sl,
    should_enter,
    update_trailing_stop,
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


def _write_open_trade(side, entry, sl, size, signal_info="", atr=0.0):
    OPEN_TRADE_FILE.write_text(json.dumps(
        {"side": side, "entry": entry, "sl": sl, "size": size,
         "signal_info": signal_info, "atr": atr, "partial_tp_done": False,
         "candles_held": 0}
    ))


def _read_open_trade() -> dict:
    if OPEN_TRADE_FILE.exists():
        try:
            return json.loads(OPEN_TRADE_FILE.read_text())
        except Exception:
            pass
    return {}


def _update_open_trade(updates: dict):
    trade = _read_open_trade()
    if trade:
        trade.update(updates)
        OPEN_TRADE_FILE.write_text(json.dumps(trade))


def _clear_open_trade():
    if OPEN_TRADE_FILE.exists():
        OPEN_TRADE_FILE.unlink()
    reset_hold_counter()


# -----------------------------------------------------------
# Main Trading Loop
# -----------------------------------------------------------

async def trading_loop(client: DydxClient):
    """
    Infinite poll loop — SuperTrend Sniper:
      1. Check pause flag
      2. Fetch candles
      3. Run SuperTrend signal generation
      4. Handle exits (SuperTrend band stop, time-based)
      5. Handle entries — every flip = trade (close + open opposite)
      6. Risk checks (daily loss, cooldown, existing position)
    """
    logger.info("=" * 60)
    logger.info("  dYdX SuperTrend Sniper — STARTED")
    logger.info(f"  Network    : {cfg.network}")
    logger.info(f"  Symbol     : {cfg.symbol}")
    logger.info(f"  TF         : {cfg.candle_resolution}")
    logger.info(f"  Interval   : {cfg.poll_interval}s")
    logger.info(f"  Dry Run    : {cfg.dry_run}")
    logger.info(f"  Strategy   : SuperTrend (ATR={cfg.st_atr_period}, Mult={cfg.st_multiplier})")
    logger.info(f"  Fee Filter : {cfg.fee_filter_enabled} ({cfg.estimated_fee_pct}%)")
    logger.info(f"  Trail ATR  : {cfg.trailing_atr_mult}x | Max Hold: {cfg.max_hold_candles} candles")
    logger.info("=" * 60)

    await send_alert(
        f"⚡ <b>dYdX SuperTrend Sniper STARTED</b>\n"
        f"Network: <code>{cfg.network.upper()}</code> | Symbol: <code>{cfg.symbol}</code>\n"
        f"Strategy: <code>SuperTrend (ATR={cfg.st_atr_period}, ×{cfg.st_multiplier})</code>\n"
        f"Fee Filter: <code>{'ON' if cfg.fee_filter_enabled else 'OFF'} ({cfg.estimated_fee_pct}%)</code>\n"
        f"Trail: <code>{cfg.trailing_atr_mult}x ATR</code>\n"
        f"Dry Run: <code>{cfg.dry_run}</code>\n\n"
        f"Send /start to open the control panel."
    )

    if cfg.dry_run:
        init_dry_run_equity(cfg.dry_run_equity)

    consecutive_errors = 0
    MAX_ERRORS = 10

    while True:
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

            # ── 2. SuperTrend signal ─────────────────────────
            result = generate_signal(
                df,
                st_atr_period=cfg.st_atr_period,
                st_multiplier=cfg.st_multiplier,
                st_use_true_atr=cfg.st_use_true_atr,
                trailing_atr_mult=cfg.trailing_atr_mult,
                max_hold_candles=cfg.max_hold_candles,
                fee_filter_enabled=cfg.fee_filter_enabled,
                estimated_fee_pct=cfg.estimated_fee_pct,
            )

            latest_close = df["close"].iloc[-1]
            signal = result.signal
            label = {1: "BUY 🟢", -1: "SELL 🔴", 0: "HOLD ⚪"}[signal]
            regime_icon = {"dead": "💀", "normal": "🟢", "volatile": "🔥"}.get(result.regime, "❓")

            logger.info(
                f"Price: ${latest_close:,.2f} | Signal: {label} | "
                f"Regime: {regime_icon} {result.regime} | "
                f"ATR: {result.atr:.2f} | "
                f"ST Band: ${result.trailing_sl:,.2f}"
            )

            # ── 3. Handle existing position ──────────────────
            current_position = await client.get_position()
            open_trade = _read_open_trade()

            if current_position is not None and open_trade:
                pos_side = current_position.get("side")
                entry_price = float(current_position.get("entryPrice", open_trade.get("entry", 0)))
                pos_size = abs(float(current_position.get("size", 0)))
                trade_atr = open_trade.get("atr", result.atr)

                # Update trailing stop using SuperTrend band
                current_tsl = get_trailing_sl()
                if result.trailing_sl > 0:
                    # Use SuperTrend band as the natural trailing stop
                    if pos_side == "LONG":
                        new_tsl = max(result.trailing_sl, current_tsl) if current_tsl > 0 else result.trailing_sl
                    else:
                        new_tsl = min(result.trailing_sl, current_tsl) if current_tsl > 0 else result.trailing_sl
                    if new_tsl != current_tsl:
                        save_trailing_sl(new_tsl)
                        logger.debug(f"Trailing SL updated: ${current_tsl:.2f} → ${new_tsl:.2f}")

                # Increment hold counter
                candles_held = increment_hold_counter()
                _update_open_trade({"candles_held": candles_held})

                # Check exit conditions
                should_exit = False
                exit_reason = ""

                # a) SuperTrend flip exit — opposite signal received
                if signal != 0:
                    if (pos_side == "LONG" and signal == -1) or \
                       (pos_side == "SHORT" and signal == 1):
                        should_exit = True
                        exit_reason = "supertrend_flip"

                # b) Time-based exit
                if candles_held >= cfg.max_hold_candles:
                    should_exit = True
                    exit_reason = f"time_exit ({candles_held} candles)"

                # c) Partial TP (close half at 1× ATR profit)
                if not open_trade.get("partial_tp_done", False) and trade_atr > 0:
                    if pos_side == "LONG" and latest_close >= entry_price + trade_atr:
                        partial_size = round(pos_size * cfg.partial_tp_pct, 4)
                        if partial_size >= 0.0001:
                            logger.info(f"📊 Partial TP hit! Closing {cfg.partial_tp_pct*100:.0f}% ({partial_size} BTC)")
                            close_result = await client.place_market_order("SELL", partial_size, reduce_only=True)
                            if close_result:
                                _update_open_trade({"partial_tp_done": True})
                                pnl = (latest_close - entry_price) * partial_size
                                await send_alert(
                                    f"📊 <b>Partial TP Hit!</b>\n"
                                    f"Closed: <code>{partial_size} BTC ({cfg.partial_tp_pct*100:.0f}%)</code>\n"
                                    f"PnL: <code>${pnl:+.2f}</code>"
                                )

                    elif pos_side == "SHORT" and latest_close <= entry_price - trade_atr:
                        partial_size = round(pos_size * cfg.partial_tp_pct, 4)
                        if partial_size >= 0.0001:
                            logger.info(f"📊 Partial TP hit! Closing {cfg.partial_tp_pct*100:.0f}% ({partial_size} BTC)")
                            close_result = await client.place_market_order("BUY", partial_size, reduce_only=True)
                            if close_result:
                                _update_open_trade({"partial_tp_done": True})
                                pnl = (entry_price - latest_close) * partial_size
                                await send_alert(
                                    f"📊 <b>Partial TP Hit!</b>\n"
                                    f"Closed: <code>{partial_size} BTC ({cfg.partial_tp_pct*100:.0f}%)</code>\n"
                                    f"PnL: <code>${pnl:+.2f}</code>"
                                )

                if should_exit:
                    logger.info(f"📤 Exit signal — closing {pos_side} | Reason: {exit_reason}")

                    close_result = await client.close_position()
                    if close_result:
                        pnl = (latest_close - entry_price) * pos_size * \
                              (1 if pos_side == "LONG" else -1)
                        pnl_pct = (pnl / entry_price / pos_size) * 100 if entry_price > 0 else 0

                        try:
                            acc = await client.get_account()
                            equity = float(acc.get("equity", 0))
                        except Exception:
                            equity = 0

                        record_trade_pnl(pnl, equity)
                        _clear_open_trade()

                        dry = close_result.get("status") == "DRY_RUN"
                        await send_alert(
                            f"{'🔵 [DRY RUN] ' if dry else ''}📤 <b>Position Closed</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"Closed : <code>{pos_side}</code>\n"
                            f"Entry  : <code>${entry_price:,.2f}</code>\n"
                            f"Exit   : <code>${latest_close:,.2f}</code>\n"
                            f"PnL    : <code>${pnl:+.2f} ({pnl_pct:+.2f}%)</code>\n"
                            f"Reason : <code>{exit_reason}</code>\n"
                            f"Held   : <code>{candles_held} candles</code>"
                        )

                    await asyncio.sleep(1)
                    current_position = None

                    # If exit was a flip, immediately re-enter opposite direction
                    if exit_reason == "supertrend_flip":
                        # Don't sleep — proceed to entry logic below
                        pass
                    else:
                        await asyncio.sleep(cfg.poll_interval)
                        continue

            # ── Handle dry-run exit ──────────────────────────
            if cfg.dry_run and open_trade:
                dry_should_exit = False
                pos_side_check = open_trade.get("side", "")

                # SuperTrend flip exit
                if signal != 0:
                    if (pos_side_check == "BUY" and signal == -1) or \
                       (pos_side_check == "SELL" and signal == 1):
                        dry_should_exit = True

                candles_held = open_trade.get("candles_held", 0)
                if candles_held >= cfg.max_hold_candles:
                    dry_should_exit = True

                if dry_should_exit:
                    dry_exit = record_dry_run_exit(latest_close)
                    if dry_exit:
                        _clear_open_trade()
                        await send_alert(
                            f"🔵 [DRY RUN] 📤 <b>Exit</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"Side   : <code>{dry_exit['side']}</code>\n"
                            f"Entry  : <code>${dry_exit['entry']:,.2f}</code>\n"
                            f"Exit   : <code>${dry_exit['exit']:,.2f}</code>\n"
                            f"PnL    : <code>${dry_exit['pnl_usdc']:+.2f} ({dry_exit['pnl_pct']:+.2f}%)</code>"
                        )

            # ── 4. Risk checks ────────────────────────────────
            if is_daily_loss_limit_hit():
                logger.warning("Daily loss limit hit — skipping cycle.")
                await asyncio.sleep(cfg.poll_interval)
                continue

            if is_cooldown_active(cfg.poll_interval):
                logger.info("⏳ Cooldown active — skipping entry.")
                await asyncio.sleep(cfg.poll_interval)
                continue

            if not should_enter(signal, current_position):
                await asyncio.sleep(cfg.poll_interval)
                consecutive_errors = 0
                continue

            # ── 5. New entry ──────────────────────────────────
            if signal != 0:
                confluence_str = ", ".join(result.confluence[:5]) if result.confluence else "none"
                await send_alert(
                    f"⚡ <b>SuperTrend Flip: {label}</b>\n"
                    f"Price: <code>${latest_close:,.2f}</code>\n"
                    f"ATR: <code>{result.atr:.2f}</code>\n"
                    f"Regime: <code>{result.regime}</code>\n"
                    f"ST Band: <code>${result.trailing_sl:,.2f}</code>\n"
                    f"Confluence: <code>{confluence_str}</code>"
                )

            # Flip: close existing if opposite
            if current_position is not None:
                pos_side = current_position.get("side")
                if (signal == 1 and pos_side == "SHORT") or \
                   (signal == -1 and pos_side == "LONG"):
                    logger.info(f"Flipping {pos_side} → closing first...")
                    await client.close_position()
                    _clear_open_trade()
                    await asyncio.sleep(1)

            # Fetch equity for sizing
            try:
                acc = await client.get_account()
                equity_usdc = float(acc.get("equity", 0))
                if equity_usdc <= 0:
                    raise ValueError("Account equity is zero or unavailable.")
            except Exception as e:
                if cfg.dry_run:
                    equity_usdc = cfg.dry_run_equity
                else:
                    logger.error(f"Could not fetch equity: {e} — skipping cycle.")
                    await asyncio.sleep(cfg.poll_interval)
                    continue

            order_side = "BUY" if signal == 1 else "SELL"
            size_btc = calculate_position_size(latest_close, equity_usdc)

            result_order = await client.place_market_order(
                side=order_side,
                size=size_btc,
            )

            if result_order:
                entry_price = latest_close
                # Use SuperTrend band as initial SL, with ATR-based fallback
                sl_price = result.trailing_sl if result.trailing_sl > 0 else \
                           calculate_sl(entry_price, order_side, atr=result.atr)

                # Save trailing SL
                save_trailing_sl(sl_price)

                logger.info(
                    f"✅ Order | Entry: ${entry_price:,.2f} | "
                    f"SL: ${sl_price:,.2f} (SuperTrend band)"
                )
                _write_open_trade(
                    order_side, entry_price, sl_price, size_btc,
                    signal_info=f"ST_flip|ATR={result.atr:.2f}",
                    atr=result.atr,
                )

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
                    f"Exit        : <code>SuperTrend flip + Trailing band</code>\n"
                    f"ATR         : <code>{result.atr:.2f}</code>\n"
                    f"Regime      : <code>{result.regime}</code>"
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
# Trailing SL Monitor (SuperTrend band-based)
# -----------------------------------------------------------

async def sl_monitor(client: DydxClient):
    """
    Checks every 5 seconds:
      1. SuperTrend trailing stop hit?
      2. Emergency SL hit?
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

            # Check trailing SL (SuperTrend band)
            effective_sl = get_trailing_sl() if get_trailing_sl() > 0 else trade.get("sl", 0)

            hit_sl = (trade["side"] == "BUY" and price <= effective_sl) or \
                     (trade["side"] == "SELL" and price >= effective_sl)

            if hit_sl:
                logger.info(f"🛑 SuperTrend Stop hit at ${price:,.2f} (SL: ${effective_sl:,.2f})")
                await client.close_position()
                pnl = (price - trade["entry"]) * trade["size"] * \
                      (1 if trade["side"] == "BUY" else -1)
                pnl_pct = (pnl / trade["entry"] / trade["size"]) * 100 if trade["entry"] > 0 else 0

                try:
                    acc = await client.get_account()
                    equity = float(acc.get("equity", 0))
                except Exception:
                    equity = 0

                record_trade_pnl(pnl, equity)
                _clear_open_trade()

                if cfg.dry_run:
                    record_dry_run_exit(price)

                await send_alert(
                    f"🛑 <b>SuperTrend Stop Hit!</b>\n"
                    f"Price: <code>${price:,.2f}</code> | SL: <code>${effective_sl:,.2f}</code>\n"
                    f"PnL: <code>${pnl:+.2f} ({pnl_pct:+.2f}%)</code>"
                )

        except Exception as e:
            logger.error(f"SL monitor error: {e}", exc_info=False)

        await asyncio.sleep(5)


# -----------------------------------------------------------
# Entry Point
# -----------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="dYdX SuperTrend Sniper Trading Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate trades without placing real orders")
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
# Keep-Alive Web Server (for Render + UptimeRobot)
# -----------------------------------------------------------

_flask_app = Flask(__name__)
_start_time = None


@_flask_app.route("/")
def index():
    return jsonify({
        "status": "running",
        "service": "dYdX SuperTrend Sniper Trading Bot",
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
        "service": "dYdX SuperTrend Sniper Trading Bot",
        "network": os.getenv("DYDX_NETWORK", "mainnet"),
        "dry_run": os.getenv("DRY_RUN", "true"),
    })


def _run_flask():
    """Run Flask in a daemon thread. Render needs an open HTTP port."""
    port = int(os.getenv("PORT", 8080))
    _flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    import time
    _start_time = time.time()

    flask_thread = threading.Thread(target=_run_flask, daemon=True, name="keep-alive")
    flask_thread.start()
    logging.getLogger("bot").info(
        f"Keep-alive server started on port {os.getenv('PORT', 8080)} "
        "→ UptimeRobot should ping /health every 10 min"
    )

    asyncio.run(main())
