"""
telegram_bot.py — Button-driven Telegram UI (no commands needed)
Everything controlled via inline keyboard buttons on your phone.

Screens:
  🏠 Dashboard  →  live price, position, uPnL, TODAY'S P&L
  📊 Status     →  full account details
  ⚡️ Settings   →  tap any setting to change it inline (Hydra Engine params)
  🛡 Risk       →  pause, resume, dry run, close position
  📅 P&L        →  today's realised P&L, trade count, loss cap usage
  📄 Logs       →  last 25 log lines
"""

import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    Bot,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from config import cfg
from risk import get_daily_pnl, get_dry_run_stats

logger = logging.getLogger(__name__)
LOG_FILE = Path("logs/bot.log")

# ── Conversation states ──────────────────────────────────────
(
    MAIN,
    SETTINGS_MENU,
    AWAITING_VALUE,
    RISK_MENU,
    LOGS_SCREEN,
    CONFIRM_CLOSE,
    PNL_SCREEN,
) = range(7)

# Which setting key is being edited (stored in context.user_data)
EDITING_KEY = "editing_key"

# ── Auth ─────────────────────────────────────────────────────

def _ok(update: Update) -> bool:
    cid = os.getenv("TELEGRAM_CHAT_ID", "")
    uid = str(
        update.effective_chat.id
        if update.effective_chat
        else update.callback_query.message.chat.id
    )
    return not cid or uid == cid.strip()

# ── Keyboard builders ─────────────────────────────────────────

def _kb(*rows):
    """Build InlineKeyboardMarkup from rows of (text, callback_data) tuples."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t, callback_data=d) for t, d in row]
        for row in rows
    ])

def main_kb():
    return _kb(
        [("📊 Status", "status"), ("💰 Balance", "balance")],
        [("📍 Position", "position"), ("📅 Today's P&L", "pnl")],
        [("⚙️ Settings", "settings"), ("🛡 Risk", "risk")],
        [("📄 Logs", "logs"), ("🔄 Refresh", "home")],
    )

def settings_kb():
    lev  = cfg.leverage
    sl   = f"{cfg.stop_loss_pct*100:.1f}%"
    tf   = cfg.candle_resolution
    net  = cfg.network.upper()
    return _kb(
        [("📐 Leverage", "set_leverage"),      (f"Now: {lev}x",                    "noop")],
        [("💵 Position Size", "set_size"),      (f"Now: {cfg.position_size_pct*100:.0f}% of equity", "noop")],
        [("🛡 Stop Loss", "set_sl"),            (f"Now: {sl}",            "noop")],
        [("⏱ Timeframe", "set_tf"),            (f"Now: {tf}",            "noop")],
        [("📊 Candle Limit", "set_limit"),      (f"Now: {cfg.candle_limit}", "noop")],
        [("⏰ Poll Interval", "set_interval"),  (f"Now: {cfg.poll_interval}s", "noop")],
        [("💸 Max Daily Loss", "set_maxloss"),  (f"Now: ${cfg.max_daily_loss_usdc}", "noop")],
        [("── 🐉 Hydra Engine ──", "noop")],
        [("🎯 Signal Threshold", "set_threshold"), (f"Now: {cfg.signal_threshold}", "noop")],
        [("📈 EMA Fast", "set_ema"),             (f"Now: {cfg.ema_fast}", "noop")],
        [("📉 RSI Period", "set_rsi"),            (f"Now: {cfg.rsi_period}", "noop")],
        [("📊 BB Period", "set_bb"),              (f"Now: {cfg.bb_period}", "noop")],
        [("🔄 Trail ATR Mult", "set_trail"),      (f"Now: {cfg.trailing_atr_mult}x", "noop")],
        [("⏱ Max Hold", "set_maxhold"),           (f"Now: {cfg.max_hold_candles}", "noop")],
        [("⏸ Cooldown", "set_cooldown"),          (f"Now: {cfg.cooldown_candles}", "noop")],
        [("🌐 Network: " + net, "toggle_network")],
        [("🏠 Back", "home")],
    )

def risk_kb():
    paused  = cfg.paused
    dry     = cfg.dry_run
    p_label = "▶️ Resume Trading" if paused else "⏸ Pause Trading"
    d_label = "🔴 Dry Run: ON"    if dry    else "🟢 Dry Run: OFF"
    return _kb(
        [(p_label, "toggle_pause")],
        [(d_label,  "toggle_dryrun")],
        [("❌ Close Position NOW", "confirm_close")],
        [("🏠 Back", "home")],
    )

def confirm_kb():
    return _kb(
        [("✅ YES — Close it", "do_close"), ("❌ Cancel", "risk")],
    )

def back_kb():
    return _kb([("🏠 Home", "home")])

def logs_kb():
    return _kb(
        [("🔄 Refresh Logs", "logs"), ("🏠 Home", "home")],
    )

def pnl_kb():
    return _kb(
        [("🔄 Refresh", "pnl"), ("🏠 Home", "home")],
    )

# ── Text builders ─────────────────────────────────────────────

def _bool_icon(v: bool) -> str:
    return "✅" if v else "❌"


def _pnl_line() -> str:
    """One-liner summary of today's P&L for embedding in other screens."""
    d = get_daily_pnl()
    net   = d.get("daily_pnl_usdc", 0.0)
    pct   = d.get("daily_pnl_pct", 0.0)
    emoji = "🟢" if net >= 0 else "🔴"
    count = d.get("trade_count", 0)
    return f"{emoji} Today's P&L: <code>${net:+.2f} ({pct:+.2f}%)</code>  ({count} trade{'s' if count != 1 else ''} closed)"

async def _dashboard_text(client=None) -> str:
    now = datetime.utcnow().strftime("%H:%M:%S UTC")
    price_str = "—"
    pos_str   = "📍 <i>No open position</i>"
    bal_str   = "—"

    if client:
        try:
            ob  = await client.get_orderbook()
            mid = (ob.get("bid", 0) + ob.get("ask", 0)) / 2
            price_str = f"${mid:,.2f}"
        except Exception:
            pass
        try:
            acc   = await client.get_account()
            equity = float(acc.get("equity", 0))
            bal_str = f"${equity:,.2f} USDC"
        except Exception:
            pass
        try:
            pos = await client.get_position()
            if pos:
                side  = pos.get("side", "?")
                size  = pos.get("size", "?")
                entry = float(pos.get("entryPrice", 0))
                upnl  = float(pos.get("unrealizedPnl", 0))
                emoji = "🟢" if upnl >= 0 else "🔴"
                pos_str = (
                    f"{emoji} <b>{side}</b> {size} BTC\n"
                    f"   Entry: <code>${entry:,.2f}</code> | uPnL: <code>${upnl:+.2f}</code>"
                )
        except Exception:
            pass

    paused_tag = " | ⏸ <b>PAUSED</b>" if cfg.paused    else ""
    dry_tag    = " | 🔵 <b>DRY RUN</b>" if cfg.dry_run  else ""

    return (
        f"🐉 <b>dYdX Hydra Bot</b>  <i>{now}</i>{paused_tag}{dry_tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 Network  : <code>{cfg.network.upper()}</code>\n"
        f"📈 Market   : <code>{cfg.symbol}</code>\n"
        f"⏱ Timeframe: <code>{cfg.candle_resolution}</code>\n"
        f"📐 Leverage : <code>{cfg.leverage}x</code>  |  💵 <code>{cfg.position_size_pct*100:.0f}% of equity</code>\n"
        f"🛡 SL: <code>{cfg.stop_loss_pct*100:.1f}%</code>  |  "
        f"📤 Exit: <code>Trailing + Score reversal</code>\n"
        f"🎯 Threshold: <code>{cfg.signal_threshold}</code>  |  "
        f"🔄 Trail: <code>{cfg.trailing_atr_mult}x ATR</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance  : <code>{bal_str}</code>\n"
        f"₿  BTC Price: <code>{price_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_pnl_line()}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{pos_str}"
    )

def _settings_text() -> str:
    return (
        "⚙️ <b>Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap any row to change its value.\n"
        "The current value is shown on the right button.\n\n"
        "<i>Changes apply on the very next trade cycle.</i>"
    )

def _risk_text() -> str:
    d     = get_daily_pnl()
    net   = d.get("daily_pnl_usdc", 0.0)
    loss  = d.get("daily_loss_usdc", 0.0)
    cap   = float(cfg.max_daily_loss_usdc)
    count = d.get("trade_count", 0)
    used_pct = (loss / cap * 100) if cap > 0 else 0
    net_emoji = "🟢" if net >= 0 else "🔴"
    return (
        "🛡 <b>Risk Controls</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Paused   : {_bool_icon(cfg.paused)}  {'Bot is NOT taking new trades.' if cfg.paused else 'Bot is trading.'}\n"
        f"Dry Run  : {_bool_icon(cfg.dry_run)}  {'No real orders sent.' if cfg.dry_run else 'LIVE orders active.'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{net_emoji} Today's Net P&L : <code>${net:+.2f} USDC</code>\n"
        f"📉 Today's Losses : <code>${loss:.2f} USDC</code>\n"
        f"🚧 Daily Loss Cap : <code>${cap:.0f} USDC</code>  ({used_pct:.0f}% used)\n"
        f"📦 Trades Closed  : <code>{count}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚠️ Force Close</b> will send a market order immediately."
    )

def _logs_text() -> str:
    if not LOG_FILE.exists():
        return "📄 <b>Logs</b>\n\n<i>No log file found yet.</i>"
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    tail  = "\n".join(lines[-25:]) if len(lines) >= 25 else "\n".join(lines)
    if len(tail) > 3600:
        tail = "…(truncated)\n" + tail[-3600:]
    return f"📄 <b>Logs</b> (last 25 lines)\n<pre>{tail}</pre>"

# ── Helpers to send/edit safely ───────────────────────────────

async def _edit(update: Update, text: str, kb: InlineKeyboardMarkup):
    """Edit the message that triggered the callback."""
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

# ── Handlers ──────────────────────────────────────────────────

# Store client reference at module level (set in start_telegram_bot)
_client = None

async def _home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update):
        return MAIN
    text = await _dashboard_text(_client)
    if update.callback_query:
        await _edit(update, text, main_kb())
    else:
        await update.message.reply_text(text, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    return MAIN

async def _status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return MAIN
    text = await _dashboard_text(_client)
    await _edit(update, text, main_kb())
    return MAIN

async def _balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return MAIN
    try:
        acc    = await _client.get_account()
        equity = float(acc.get("equity", 0))
        free   = float(acc.get("freeCollateral", 0))
        text = (
            f"💰 <b>Account Balance</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total Equity     : <code>${equity:,.2f} USDC</code>\n"
            f"Free Collateral  : <code>${free:,.2f} USDC</code>"
        )
    except Exception as e:
        text = f"❌ Could not fetch balance:\n<code>{e}</code>"
    await _edit(update, text, back_kb())
    return MAIN

async def _position(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return MAIN
    try:
        pos = await _client.get_position()
        if pos is None:
            text = "📍 <b>Position</b>\n\n<i>No open position.</i>"
        else:
            side  = pos.get("side", "?")
            size  = pos.get("size", "?")
            entry = float(pos.get("entryPrice", 0))
            upnl  = float(pos.get("unrealizedPnl", 0))
            liq   = pos.get("liquidationPrice", "N/A")
            emoji = "🟢" if upnl >= 0 else "🔴"
            text = (
                f"📍 <b>Open Position</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Side        : {emoji} <b>{side}</b>\n"
                f"Size        : <code>{size} BTC</code>\n"
                f"Entry Price : <code>${entry:,.2f}</code>\n"
                f"Unrealized PnL : <code>${upnl:+.2f} USDC</code>\n"
                f"Liq. Price  : <code>${liq}</code>"
            )
    except Exception as e:
        text = f"❌ Error:\n<code>{e}</code>"
    await _edit(update, text, back_kb())
    return MAIN

async def _settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return SETTINGS_MENU
    await _edit(update, _settings_text(), settings_kb())
    return SETTINGS_MENU

async def _risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return RISK_MENU
    await _edit(update, _risk_text(), risk_kb())
    return RISK_MENU

async def _logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return MAIN
    await _edit(update, _logs_text(), logs_kb())
    return MAIN


async def _pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dedicated Today's P&L screen with percentage and dry-run stats."""
    if not _ok(update): return MAIN
    d     = get_daily_pnl()
    net   = d.get("daily_pnl_usdc", 0.0)
    net_pct = d.get("daily_pnl_pct", 0.0)
    loss  = d.get("daily_loss_usdc", 0.0)
    wins  = d.get("trade_count", 0)   # total closed trades
    cap   = float(cfg.max_daily_loss_usdc)
    used_pct = (loss / cap * 100) if cap > 0 else 0
    remaining = max(cap - loss, 0)
    today = d.get("date", "today")

    net_emoji = "🟢 PROFIT" if net > 0 else ("🔴 LOSS" if net < 0 else "⚪ BREAK-EVEN")

    # Progress bar for daily loss cap  (10 blocks)
    filled = min(int(used_pct / 10), 10)
    bar = "🟥" * filled + "⬜" * (10 - filled)

    text = (
        f"📅 <b>Today's Trading Summary</b>\n"
        f"<i>{today} (UTC)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Net P&L   :</b>  <code>${net:+.2f} ({net_pct:+.2f}%)</code>  {net_emoji}\n"
        f"<b>Realised  :</b>  <code>{wins}</code> trade{'s' if wins != 1 else ''} closed\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Daily Loss Cap</b>\n"
        f"{bar}  <code>{used_pct:.0f}%</code>\n"
        f"Used      : <code>${loss:.2f}</code> of <code>${cap:.0f}</code>\n"
        f"Remaining : <code>${remaining:.2f} USDC</code>\n"
    )

    # Dry-run section
    if cfg.dry_run:
        dr = get_dry_run_stats()
        dr_eq = dr.get("equity", 0.0)
        dr_start = dr.get("starting_equity", 0.0)
        dr_pnl = dr.get("daily_pnl_usdc", 0.0)
        dr_pnl_pct = dr.get("daily_pnl_pct", 0.0)
        dr_trades = dr.get("trade_count", 0)
        dr_wins = dr.get("win_count", 0)
        dr_wr = dr.get("win_rate", 0.0)
        dr_open = dr.get("open_trade")
        total_change = dr_eq - dr_start if dr_start > 0 else 0
        total_pct = (total_change / dr_start * 100) if dr_start > 0 else 0

        text += (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔵 <b>Dry-Run Simulation</b>\n"
            f"Start Equity : <code>${dr_start:,.2f}</code>\n"
            f"Current      : <code>${dr_eq:,.2f} ({total_pct:+.2f}%)</code>\n"
            f"Today's P&L  : <code>${dr_pnl:+.2f} ({dr_pnl_pct:+.2f}%)</code>\n"
            f"Trades       : <code>{dr_trades}</code> (W: {dr_wins} | WR: {dr_wr:.0f}%)\n"
        )
        if dr_open:
            text += f"Open Trade   : <code>{dr_open['side']} @ ${dr_open['entry']:,.2f}</code>\n"

    text += (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Resets automatically at midnight UTC.</i>"
    )
    await _edit(update, text, pnl_kb())
    return PNL_SCREEN

# ── Setting input flow ────────────────────────────────────────

_SET_PROMPTS = {
    "set_leverage":  ("set_leverage",  "leverage",             "📐 Enter new leverage (1–20):\nExample: <code>5</code>"),
    "set_size":      ("set_size",      "position_size_pct",    "💵 Enter position size as % of equity:\nExample: <code>10</code> for 10%  (min 1%, max 100%)"),
    "set_sl":        ("set_sl",        "stop_loss_pct",        "🛡 Enter stop loss % (emergency SL):\nExample: <code>1.5</code> for 1.5%"),
    "set_tf":        ("set_tf",        "candle_resolution",    "⏱ Enter timeframe:\n<code>1MIN  5MINS  15MINS  30MINS  1HOUR  4HOURS  1DAY</code>"),
    "set_limit":     ("set_limit",     "candle_limit",         "📊 Enter candle limit:\nExample: <code>100</code>"),
    "set_interval":  ("set_interval",  "poll_interval",        "⏰ Enter poll interval in seconds:\nExample: <code>60</code>"),
    "set_maxloss":   ("set_maxloss",   "max_daily_loss_usdc",  "💸 Enter max daily loss (USDC):\nExample: <code>100</code>"),
    "set_threshold": ("set_threshold", "signal_threshold",     "🎯 Signal threshold (20–95):\nHigher = fewer but stronger signals\nExample: <code>60</code>"),
    "set_ema":       ("set_ema",       "ema_fast",             "📈 EMA fast period (3–20):\nExample: <code>8</code>"),
    "set_rsi":       ("set_rsi",       "rsi_period",           "📉 RSI period (3–21):\nExample: <code>7</code>"),
    "set_bb":        ("set_bb",        "bb_period",            "📊 Bollinger Band period (10–50):\nExample: <code>20</code>"),
    "set_trail":     ("set_trail",     "trailing_atr_mult",    "🔄 Trailing stop ATR multiplier (0.5–5.0):\nExample: <code>1.5</code>"),
    "set_maxhold":   ("set_maxhold",   "max_hold_candles",     "⏱ Max candles to hold a position (3–120):\nExample: <code>15</code>"),
    "set_cooldown":  ("set_cooldown",  "cooldown_candles",     "⏸ Cooldown candles after a loss (0–20):\nExample: <code>2</code>"),
}

async def _start_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return SETTINGS_MENU
    cb = update.callback_query.data
    if cb not in _SET_PROMPTS:
        await update.callback_query.answer()
        return SETTINGS_MENU

    _, cfg_key, prompt = _SET_PROMPTS[cb]
    ctx.user_data[EDITING_KEY] = cfg_key

    # For pct fields, show current value in readable %
    cur = getattr(cfg, cfg_key)
    if cfg_key in ("stop_loss_pct", "take_profit_pct"):
        cur_str = f"{cur*100:.2f}%"
    else:
        cur_str = str(cur)

    text = (
        f"{prompt}\n\n"
        f"Current value: <code>{cur_str}</code>\n\n"
        f"<i>Type your new value below 👇</i>"
    )
    cancel_kb = _kb([("❌ Cancel", "settings")])
    await _edit(update, text, cancel_kb)
    return AWAITING_VALUE

async def _receive_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return SETTINGS_MENU
    raw     = update.message.text.strip()
    cfg_key = ctx.user_data.get(EDITING_KEY)

    if not cfg_key:
        await update.message.reply_text("❌ Session lost. Go back to settings.", reply_markup=back_kb())
        return MAIN

    # For pct fields user types e.g. "10" meaning 10% → store as 0.10
    # Also handle "1.5" → 0.015 etc.
    try:
        if cfg_key in ("stop_loss_pct", "take_profit_pct", "position_size_pct"):
            numeric = float(raw)
            if numeric > 1.0:
                numeric = numeric / 100.0
            result = cfg.set(cfg_key, numeric)
        else:
            result = cfg.set(cfg_key, raw)

        text = (
            f"✅ <b>Updated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{result}\n\n"
            f"<i>Change takes effect on the next trade cycle.</i>"
        )
    except ValueError as e:
        text = f"❌ <b>Invalid value</b>\n\n<code>{e}</code>"

    ctx.user_data.pop(EDITING_KEY, None)
    await update.message.reply_text(text, reply_markup=settings_kb(), parse_mode=ParseMode.HTML)
    return SETTINGS_MENU

# ── Toggle actions ────────────────────────────────────────────

async def _toggle_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return RISK_MENU
    cfg.set("paused", not cfg.paused)
    await _edit(update, _risk_text(), risk_kb())
    return RISK_MENU

async def _toggle_dryrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return RISK_MENU
    cfg.set("dry_run", not cfg.dry_run)
    state = "ON 🔵" if cfg.dry_run else "OFF 🟢 (LIVE)"
    text = _risk_text() + f"\n\n<b>Dry Run is now {state}</b>"
    await _edit(update, text, risk_kb())
    return RISK_MENU

async def _toggle_network(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return SETTINGS_MENU
    new_net = "testnet" if cfg.network == "mainnet" else "mainnet"
    cfg.set("network", new_net)
    text = (
        f"🌐 <b>Network switched!</b>\n\n"
        f"Now: <code>{new_net.upper()}</code>\n\n"
        f"⚠️ Reconnect required. On your Oracle VM run:\n"
        f"<code>sudo systemctl restart cryptotrade</code>"
    )
    await _edit(update, text, _kb([("🏠 Home", "home"), ("⚙️ Settings", "settings")]))
    return SETTINGS_MENU

# ── Close position flow ───────────────────────────────────────

async def _confirm_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return RISK_MENU
    text = (
        "⚠️ <b>Confirm Close Position</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "This will send a <b>market order</b> to close your\n"
        "current position immediately.\n\n"
        "Are you sure?"
    )
    await _edit(update, text, confirm_kb())
    return CONFIRM_CLOSE

async def _do_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ok(update): return RISK_MENU
    await update.callback_query.answer("Closing position…")
    try:
        result = await _client.close_position()
        if result is None:
            text = "ℹ️ <b>No open position</b> to close."
        elif result.get("status") == "DRY_RUN":
            text = "🔵 <b>[DRY RUN]</b> Position close simulated. No real order sent."
        else:
            tx = result.get("tx_hash", "N/A")
            text = f"✅ <b>Position Closed</b>\n\nTX Hash:\n<code>{tx}</code>"
    except Exception as e:
        text = f"❌ <b>Close failed</b>\n\n<code>{e}</code>"
    await _edit(update, text, _kb([("🏠 Home", "home"), ("🛡 Risk", "risk")]))
    return MAIN

async def _noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Absorb taps on 'current value' display buttons."""
    await update.callback_query.answer("ℹ️ Tap the label on the left to change this value.")
    return SETTINGS_MENU

# ── Conversation wiring ───────────────────────────────────────

def build_conversation() -> ConversationHandler:
    """Build the full ConversationHandler with all states and transitions."""
    set_triggers = [CallbackQueryHandler(_start_input, pattern=f"^{k}$") for k in _SET_PROMPTS]

    return ConversationHandler(
        entry_points=[CommandHandler("start", _home)],
        states={
            MAIN: [
                CallbackQueryHandler(_home,          pattern="^home$"),
                CallbackQueryHandler(_status,        pattern="^status$"),
                CallbackQueryHandler(_balance,       pattern="^balance$"),
                CallbackQueryHandler(_position,      pattern="^position$"),
                CallbackQueryHandler(_pnl,           pattern="^pnl$"),
                CallbackQueryHandler(_settings,      pattern="^settings$"),
                CallbackQueryHandler(_risk,          pattern="^risk$"),
                CallbackQueryHandler(_logs,          pattern="^logs$"),
            ],
            SETTINGS_MENU: [
                *set_triggers,
                CallbackQueryHandler(_toggle_network, pattern="^toggle_network$"),
                CallbackQueryHandler(_home,           pattern="^home$"),
                CallbackQueryHandler(_noop,           pattern="^noop$"),
                CallbackQueryHandler(_settings,       pattern="^settings$"),
            ],
            AWAITING_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_value),
                CallbackQueryHandler(_settings, pattern="^settings$"),
            ],
            RISK_MENU: [
                CallbackQueryHandler(_toggle_pause,   pattern="^toggle_pause$"),
                CallbackQueryHandler(_toggle_dryrun,  pattern="^toggle_dryrun$"),
                CallbackQueryHandler(_confirm_close,  pattern="^confirm_close$"),
                CallbackQueryHandler(_home,           pattern="^home$"),
                CallbackQueryHandler(_risk,           pattern="^risk$"),
            ],
            CONFIRM_CLOSE: [
                CallbackQueryHandler(_do_close, pattern="^do_close$"),
                CallbackQueryHandler(_risk,     pattern="^risk$"),
            ],
            LOGS_SCREEN: [
                CallbackQueryHandler(_logs, pattern="^logs$"),
                CallbackQueryHandler(_home, pattern="^home$"),
            ],
            PNL_SCREEN: [
                CallbackQueryHandler(_pnl,  pattern="^pnl$"),
                CallbackQueryHandler(_home, pattern="^home$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", _home),
            CallbackQueryHandler(_home, pattern="^home$"),
        ],
        per_message=False,
        allow_reentry=True,
    )

# ── Startup & push alerts ─────────────────────────────────────

async def start_telegram_bot(client) -> None:
    global _client
    _client = client

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram UI disabled.")
        return

    app = Application.builder().token(token).build()
    app.add_handler(build_conversation())

    await app.bot.set_my_commands([
        BotCommand("start", "Open the control dashboard"),
    ])

    logger.info("✅ Telegram button UI started. Send /start to your bot.")

    await app.initialize()
    await app.start()

    # drop_pending_updates=True kicks any existing getUpdates session off
    # Telegram's servers before this instance starts polling.  This prevents
    # the "Conflict: terminated by other getUpdates request" error when Render
    # restarts a service or you accidentally start a second local instance.
    await app.updater.start_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )

    logger.info("Telegram polling started (stale sessions dropped).")

    try:
        await asyncio.Event().wait()
    except Exception as e:
        from telegram.error import Conflict
        if isinstance(e, Conflict):
            logger.critical(
                "Telegram Conflict error: another bot instance is already running. "
                "Kill all other instances before starting this one."
            )
        raise
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def send_alert(message: str) -> None:
    """Push a notification to the owner's chat from the trading loop."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        bot = Bot(token)
        await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")
