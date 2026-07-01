"""
telegram_bot.py — Telegram Control Interface
Lets you control every aspect of the trading bot from Telegram.

Commands:
  /start        - Welcome & quick status
  /status       - Full bot status + current position
  /balance      - Account USDC balance
  /position     - Open position details
  /set <k> <v>  - Change a setting (leverage, sl, tp, etc.)
  /settings     - Show all current settings
  /pause        - Pause trading (no new entries)
  /resume       - Resume trading
  /close        - Force-close current position NOW
  /dryrun       - Toggle dry-run mode
  /network      - Switch mainnet ↔ testnet
  /logs         - Last 25 log lines
  /help         - This message
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Awaitable

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import cfg

if TYPE_CHECKING:
    from dydx_client import DydxClient

logger = logging.getLogger(__name__)

LOG_FILE = Path("logs/bot.log")


# -----------------------------------------------------------
# Auth Guard — only respond to the owner's chat ID
# -----------------------------------------------------------

def _authorized(update: Update) -> bool:
    allowed = os.getenv("TELEGRAM_CHAT_ID", "")
    if not allowed:
        return True  # No restriction set — allow all (dev mode)
    return str(update.effective_chat.id) == allowed.strip()


def owner_only(handler: Callable) -> Callable:
    """Decorator: silently ignore messages from non-owners."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(update):
            logger.warning(
                f"Unauthorized Telegram access attempt from chat_id={update.effective_chat.id}"
            )
            await update.message.reply_text("⛔ Unauthorized.")
            return
        await handler(update, ctx)
    wrapper.__name__ = handler.__name__
    return wrapper


# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

def _fmt_bool(v: bool) -> str:
    return "✅ ON" if v else "❌ OFF"


def _tail_log(n: int = 25) -> str:
    if not LOG_FILE.exists():
        return "_No log file yet._"
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    tail = lines[-n:] if len(lines) >= n else lines
    return "\n".join(tail)


# -----------------------------------------------------------
# Command Handlers
# -----------------------------------------------------------

@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *dYdX SMC Trading Bot*\n\n"
        f"Network : `{cfg.network}`\n"
        f"Symbol  : `{cfg.symbol}`\n"
        f"Paused  : {_fmt_bool(cfg.paused)}\n"
        f"Dry Run : {_fmt_bool(cfg.dry_run)}\n\n"
        "Type /help to see all commands."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📋 *Available Commands*\n\n"
        "/status — Full bot status + position\n"
        "/balance — USDC account balance\n"
        "/position — Open position details\n"
        "/settings — All current settings\n"
        "/set `<key>` `<value>` — Change a setting\n"
        "/pause — Pause trading\n"
        "/resume — Resume trading\n"
        "/close — Force-close position now\n"
        "/dryrun — Toggle dry-run mode\n"
        "/network — Switch mainnet ↔ testnet\n"
        "/logs — Last 25 log lines\n\n"
        "⚙️ *Settable Keys*\n"
        "`leverage` — e.g. /set leverage 5\n"
        "`position_size_usdc` — e.g. /set position_size_usdc 100\n"
        "`stop_loss_pct` — e.g. /set stop_loss_pct 0.02\n"
        "`take_profit_pct` — e.g. /set take_profit_pct 0.04\n"
        "`candle_resolution` — e.g. /set candle_resolution 5MINS\n"
        "`poll_interval` — e.g. /set poll_interval 30\n"
        "`max_daily_loss_usdc` — e.g. /set max_daily_loss_usdc 200\n"
        "`dry_run` — e.g. /set dry_run false\n"
        "`network` — e.g. /set network testnet\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    snap = cfg.snapshot()
    lines = ["⚙️ *Current Settings*\n"]
    skip = {"log_level"}
    for k, v in snap.items():
        if k in skip:
            continue
        if isinstance(v, bool):
            lines.append(f"`{k}`: {_fmt_bool(v)}")
        elif isinstance(v, float):
            lines.append(f"`{k}`: `{v}`")
        else:
            lines.append(f"`{k}`: `{v}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /set <key> <value>"""
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: `/set <key> <value>`\nExample: `/set leverage 5`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    key = args[0].strip().lower()
    value = " ".join(args[1:]).strip()

    try:
        result = cfg.set(key, value)
        # If network changed, warn that reconnect is needed
        if key == "network":
            result += "\n\n⚠️ Network change requires a bot restart to take effect."
        await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"[Telegram] Config updated: {key} = {value}")
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg.set("paused", True)
    await update.message.reply_text(
        "⏸ *Bot PAUSED*\nNo new trades will be entered. Existing position is unaffected.\nUse /resume to restart.",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("[Telegram] Bot paused by user.")


@owner_only
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg.set("paused", False)
    await update.message.reply_text(
        "▶️ *Bot RESUMED*\nTrading is now active.",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("[Telegram] Bot resumed by user.")


@owner_only
async def cmd_dryrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    new_val = not cfg.dry_run
    cfg.set("dry_run", new_val)
    status = "ON — no real orders will be placed" if new_val else "OFF — LIVE TRADING ACTIVE"
    await update.message.reply_text(
        f"🔁 *Dry Run toggled*\nDry Run is now: `{status}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info(f"[Telegram] Dry run set to {new_val}")


@owner_only
async def cmd_network(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = cfg.network
    new_net = "testnet" if current == "mainnet" else "mainnet"
    cfg.set("network", new_net)
    await update.message.reply_text(
        f"🌐 *Network switched*\n`{current}` → `{new_net}`\n\n"
        "⚠️ Restart the bot for this to take effect:\n`sudo systemctl restart cryptotrade`",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info(f"[Telegram] Network switched from {current} to {new_net}")


@owner_only
async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tail = _tail_log(25)
    # Telegram message limit is 4096 chars
    if len(tail) > 3800:
        tail = "...(truncated)\n" + tail[-3800:]
    await update.message.reply_text(
        f"📄 *Last log lines:*\n```\n{tail}\n```",
        parse_mode=ParseMode.MARKDOWN,
    )


# -----------------------------------------------------------
# Commands that need dYdX client access
# These are registered with a closure that captures `client`
# -----------------------------------------------------------

def make_status_handler(client):
    @owner_only
    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            account = await client.get_account()
            position = await client.get_position()
            ob = await client.get_orderbook()

            equity = float(account.get("equity", 0))
            free_col = float(account.get("freeCollateral", 0))
            price = ob.get("bid", "N/A")

            pos_text = "_No open position_"
            if position:
                side = position.get("side", "?")
                size = position.get("size", "?")
                entry = float(position.get("entryPrice", 0))
                upnl = float(position.get("unrealizedPnl", 0))
                emoji = "🟢" if upnl >= 0 else "🔴"
                pos_text = (
                    f"{emoji} *{side}* `{size}` BTC\n"
                    f"Entry: `${entry:,.2f}` | uPnL: `${upnl:+.2f}`"
                )

            msg = (
                f"📊 *Bot Status — {datetime.utcnow().strftime('%H:%M:%S UTC')}*\n\n"
                f"🌐 Network  : `{cfg.network}`\n"
                f"📈 Symbol   : `{cfg.symbol}`\n"
                f"⏱ TF       : `{cfg.candle_resolution}`\n"
                f"⚙️ Leverage : `{cfg.leverage}x`\n"
                f"💵 Size     : `${cfg.position_size_usdc} USDC`\n"
                f"🛡 SL/TP   : `{cfg.stop_loss_pct*100:.1f}%` / `{cfg.take_profit_pct*100:.1f}%`\n"
                f"⏸ Paused   : {_fmt_bool(cfg.paused)}\n"
                f"🔁 Dry Run  : {_fmt_bool(cfg.dry_run)}\n\n"
                f"💰 *Account*\n"
                f"Equity    : `${equity:,.2f}`\n"
                f"Free Col. : `${free_col:,.2f}`\n"
                f"BTC Price : `${price:,.2f}`\n\n"
                f"📍 *Position*\n{pos_text}"
            )
        except Exception as e:
            msg = f"❌ Could not fetch status: `{e}`"

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return cmd_status


def make_balance_handler(client):
    @owner_only
    async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            account = await client.get_account()
            equity = float(account.get("equity", 0))
            free = float(account.get("freeCollateral", 0))
            msg = (
                f"💰 *Account Balance*\n\n"
                f"Total Equity  : `${equity:,.2f} USDC`\n"
                f"Free Collateral: `${free:,.2f} USDC`"
            )
        except Exception as e:
            msg = f"❌ Error: `{e}`"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return cmd_balance


def make_position_handler(client):
    @owner_only
    async def cmd_position(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            position = await client.get_position()
            if position is None:
                msg = "📍 *No open position.*"
            else:
                side = position.get("side", "?")
                size = position.get("size", "?")
                entry = float(position.get("entryPrice", 0))
                upnl = float(position.get("unrealizedPnl", 0))
                liq_price = position.get("liquidationPrice", "N/A")
                emoji = "🟢" if upnl >= 0 else "🔴"
                msg = (
                    f"📍 *Open Position*\n\n"
                    f"Side      : {emoji} `{side}`\n"
                    f"Size      : `{size} BTC`\n"
                    f"Entry     : `${entry:,.2f}`\n"
                    f"uPnL      : `${upnl:+.2f} USDC`\n"
                    f"Liq. Price: `${liq_price}`"
                )
        except Exception as e:
            msg = f"❌ Error: `{e}`"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return cmd_position


def make_close_handler(client):
    @owner_only
    async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚡ Closing position...")
        try:
            result = await client.close_position()
            if result is None:
                msg = "ℹ️ No open position to close."
            else:
                dry = result.get("status") == "DRY_RUN"
                msg = (
                    "✅ Position closed (DRY RUN — no real order sent)."
                    if dry else
                    f"✅ Position closed.\nTX: `{result.get('tx_hash', 'N/A')}`"
                )
        except Exception as e:
            msg = f"❌ Failed to close: `{e}`"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return cmd_close


# -----------------------------------------------------------
# Bot Lifecycle
# -----------------------------------------------------------

async def start_telegram_bot(client) -> None:
    """
    Build and start the Telegram bot application.
    Runs as a concurrent asyncio task — does NOT block.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set — Telegram control disabled. "
            "Set it in .env to enable."
        )
        return

    app = Application.builder().token(token).build()

    # Register static handlers
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set",      cmd_set))
    app.add_handler(CommandHandler("pause",    cmd_pause))
    app.add_handler(CommandHandler("resume",   cmd_resume))
    app.add_handler(CommandHandler("dryrun",   cmd_dryrun))
    app.add_handler(CommandHandler("network",  cmd_network))
    app.add_handler(CommandHandler("logs",     cmd_logs))

    # Register handlers that need the dYdX client
    app.add_handler(CommandHandler("status",   make_status_handler(client)))
    app.add_handler(CommandHandler("balance",  make_balance_handler(client)))
    app.add_handler(CommandHandler("position", make_position_handler(client)))
    app.add_handler(CommandHandler("close",    make_close_handler(client)))

    # Set the command list visible in Telegram menu
    await app.bot.set_my_commands([
        BotCommand("start",    "Welcome & quick status"),
        BotCommand("status",   "Full bot status + position"),
        BotCommand("balance",  "Account USDC balance"),
        BotCommand("position", "Open position details"),
        BotCommand("set",      "Change a setting: /set leverage 5"),
        BotCommand("settings", "Show all settings"),
        BotCommand("pause",    "Pause trading"),
        BotCommand("resume",   "Resume trading"),
        BotCommand("close",    "Force-close position now"),
        BotCommand("dryrun",   "Toggle dry-run mode"),
        BotCommand("network",  "Switch mainnet ↔ testnet"),
        BotCommand("logs",     "Last 25 log lines"),
        BotCommand("help",     "Show all commands"),
    ])

    logger.info("✅ Telegram bot started. Send /start to your bot.")

    # Start polling (non-blocking — uses the running event loop)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message"])

    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def send_alert(message: str) -> None:
    """
    Send a push notification to the owner's chat.
    Call this from the trading loop for important events.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        from telegram import Bot
        bot = Bot(token)
        await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")
