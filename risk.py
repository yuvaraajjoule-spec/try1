"""
risk.py — Risk Management Layer
Handles position sizing, stop-loss/take-profit, and daily loss limits.
Also exposes get_daily_pnl() for the Telegram dashboard.
"""

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

STATE_FILE = Path("state.json")


# -----------------------------------------------------------
# State Persistence (tracks daily P&L across restarts)
# -----------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "date": str(date.today()),
        "daily_pnl_usdc": 0.0,   # net P&L (positive = profit, negative = loss)
        "daily_loss_usdc": 0.0,  # cumulative loss only (for loss-limit guard)
        "trade_count": 0,
    }


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_daily_state() -> dict:
    state = _load_state()
    if state.get("date") != str(date.today()):
        # New day — reset all daily counters
        state = {
            "date": str(date.today()),
            "daily_pnl_usdc": 0.0,
            "daily_loss_usdc": 0.0,
            "trade_count": 0,
        }
        _save_state(state)
    return state


def record_trade_pnl(pnl_usdc: float):
    """Call this after a trade closes to track daily net P&L and loss limit."""
    state = _get_daily_state()
    state["daily_pnl_usdc"]  = round(state.get("daily_pnl_usdc", 0.0) + pnl_usdc, 4)
    state["trade_count"]     = state.get("trade_count", 0) + 1
    if pnl_usdc < 0:
        state["daily_loss_usdc"] += abs(pnl_usdc)
    _save_state(state)
    logger.info(
        f"Trade #{state['trade_count']} closed | "
        f"PnL: ${pnl_usdc:+.2f} | "
        f"Day net: ${state['daily_pnl_usdc']:+.2f} USDC"
    )


def get_daily_pnl() -> dict:
    """
    Return today's trading summary.
    Safe to call from anywhere (Telegram UI, dashboard, etc.).

    Returns:
        dict with keys: date, daily_pnl_usdc, daily_loss_usdc, trade_count
    """
    return _get_daily_state()


# -----------------------------------------------------------
# Position Sizing
# -----------------------------------------------------------

def calculate_position_size(btc_price: float, equity_usdc: float) -> float:
    """
    Convert equity fraction + leverage → BTC contract size.

    Formula:
        collateral_usdc = equity_usdc × POSITION_SIZE_PCT
        size_btc        = (collateral_usdc × LEVERAGE) / btc_price

    Leverage scales the contract size (how many BTC you control),
    but has NO effect on the SL/TP price levels — those are purely
    entry_price ± percentage, computed in calculate_sl_tp().

    dYdX BTC-USD contract step size = 0.0001 BTC.
    Reads from cfg singleton so Telegram-changed values apply live.
    """
    from config import cfg  # late import to avoid circular dependency
    pct      = float(cfg.position_size_pct)   # e.g. 0.10 for 10%
    leverage = float(cfg.leverage)
    min_step = 0.0001

    collateral_usdc = equity_usdc * pct
    size_btc = (collateral_usdc * leverage) / btc_price
    # Round down to nearest step size
    size_btc = round(size_btc - (size_btc % min_step), 4)

    if size_btc < min_step:
        raise ValueError(
            f"Calculated size {size_btc} BTC is below minimum {min_step}. "
            f"Equity: ${equity_usdc:.2f} | Pct: {pct*100:.0f}% | "
            f"Leverage: {leverage}x | BTC price: ${btc_price:.2f}. "
            f"Increase POSITION_SIZE_PCT or LEVERAGE."
        )

    logger.debug(
        f"Position size: {size_btc} BTC "
        f"(equity ${equity_usdc:.2f} × {pct*100:.0f}% = ${collateral_usdc:.2f} collateral "
        f"× {leverage}x lev @ ${btc_price:.2f})"
    )
    return size_btc


# -----------------------------------------------------------
# Stop Loss / Take Profit
# -----------------------------------------------------------

def calculate_sl_tp(
    entry_price: float,
    side: str,          # "BUY" or "SELL"
    df: Optional[pd.DataFrame] = None,
) -> Tuple[float, float]:
    """
    Calculate stop-loss and take-profit prices.

    Uses swing-based levels if df is provided, else falls back
    to the percentage levels in .env.

    Args:
        entry_price: Filled price of the order.
        side: "BUY" (long) or "SELL" (short).
        df: Processed SMC DataFrame with swing_high/swing_low columns.

    Returns:
        (stop_loss_price, take_profit_price)
    """
    from config import cfg  # late import to avoid circular dependency
    sl_pct = float(cfg.stop_loss_pct)
    tp_pct = float(cfg.take_profit_pct)

    # Try swing-based SL/TP
    if df is not None and "swing_high" in df.columns and "swing_low" in df.columns:
        recent_lows = df["swing_low"].dropna()
        recent_highs = df["swing_high"].dropna()

        if side == "BUY" and len(recent_lows) > 0:
            swing_sl = recent_lows.iloc[-1]
            # Only use swing SL if it's tighter than our max risk
            if swing_sl > entry_price * (1 - sl_pct * 2):
                actual_sl_pct = (entry_price - swing_sl) / entry_price
                sl = swing_sl
                tp = entry_price * (1 + actual_sl_pct * 2)  # 1:2 RR
                logger.debug(f"Swing-based SL: {sl:.2f}, TP: {tp:.2f}")
                return round(sl, 2), round(tp, 2)

        elif side == "SELL" and len(recent_highs) > 0:
            swing_sl = recent_highs.iloc[-1]
            if swing_sl < entry_price * (1 + sl_pct * 2):
                actual_sl_pct = (swing_sl - entry_price) / entry_price
                sl = swing_sl
                tp = entry_price * (1 - actual_sl_pct * 2)
                logger.debug(f"Swing-based SL: {sl:.2f}, TP: {tp:.2f}")
                return round(sl, 2), round(tp, 2)

    # Fallback: fixed percentage
    if side == "BUY":
        sl = entry_price * (1 - sl_pct)
        tp = entry_price * (1 + tp_pct)
    else:
        sl = entry_price * (1 + sl_pct)
        tp = entry_price * (1 - tp_pct)

    logger.debug(f"Pct-based SL: {sl:.2f}, TP: {tp:.2f}")
    return round(sl, 2), round(tp, 2)


# -----------------------------------------------------------
# Daily Loss Guard
# -----------------------------------------------------------

def is_daily_loss_limit_hit() -> bool:
    """Returns True if today's loss has exceeded MAX_DAILY_LOSS_USDC."""
    from config import cfg  # late import to avoid circular dependency
    max_loss = float(cfg.max_daily_loss_usdc)
    state = _get_daily_state()
    loss = state.get("daily_loss_usdc", 0.0)

    if loss >= max_loss:
        logger.warning(
            f"🛑 Daily loss limit hit: ${loss:.2f} >= ${max_loss:.2f}. "
            f"No new trades until tomorrow."
        )
        return True
    return False


# -----------------------------------------------------------
# Entry Guard
# -----------------------------------------------------------

def should_enter(signal: int, current_position: Optional[dict]) -> bool:
    """
    Decide if we should act on a signal given the current position.

    signal: 1 = BUY, -1 = SELL, 0 = HOLD
    Returns True if we should enter/flip/close.
    """
    if signal == 0:
        return False

    if current_position is None:
        # No position — enter on any non-zero signal
        return True

    pos_side = current_position.get("side", "")
    if signal == 1 and pos_side == "LONG":
        logger.debug("Already LONG — skipping BUY signal.")
        return False
    if signal == -1 and pos_side == "SHORT":
        logger.debug("Already SHORT — skipping SELL signal.")
        return False

    # Opposite signal — we'll flip (close existing + open new)
    return True
