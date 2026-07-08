"""
risk.py — Risk Management Layer
Handles position sizing, stop-loss, daily loss limits, and dry-run P&L tracking.
Also exposes get_daily_pnl() and get_daily_pnl_pct() for the Telegram dashboard.
"""

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional, Tuple, List

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
        "daily_pnl_usdc": 0.0,     # net P&L (positive = profit, negative = loss)
        "daily_pnl_pct": 0.0,      # net P&L as % of equity
        "daily_loss_usdc": 0.0,    # cumulative loss only (for loss-limit guard)
        "trade_count": 0,
        "trades": [],              # per-trade details
        # Dry-run simulation state
        "dry_run_equity": 0.0,     # current simulated equity
        "dry_run_starting_equity": 0.0,
        "dry_run_open_trade": None,  # {"side", "entry", "size"}
        "dry_run_pnl_usdc": 0.0,
        "dry_run_pnl_pct": 0.0,
        "dry_run_trades": [],
    }


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_daily_state() -> dict:
    state = _load_state()
    if state.get("date") != str(date.today()):
        # New day — reset all daily counters, preserve dry-run equity
        dry_eq = state.get("dry_run_equity", 0.0)
        dry_start = state.get("dry_run_starting_equity", 0.0)
        dry_open = state.get("dry_run_open_trade")
        state = {
            "date": str(date.today()),
            "daily_pnl_usdc": 0.0,
            "daily_pnl_pct": 0.0,
            "daily_loss_usdc": 0.0,
            "trade_count": 0,
            "trades": [],
            "dry_run_equity": dry_eq,
            "dry_run_starting_equity": dry_eq if dry_eq > 0 else dry_start,
            "dry_run_open_trade": dry_open,
            "dry_run_pnl_usdc": 0.0,
            "dry_run_pnl_pct": 0.0,
            "dry_run_trades": [],
        }
        _save_state(state)
    return state


def record_trade_pnl(pnl_usdc: float, equity: float = 0.0):
    """Call this after a trade closes to track daily net P&L and loss limit."""
    state = _get_daily_state()
    state["daily_pnl_usdc"] = round(state.get("daily_pnl_usdc", 0.0) + pnl_usdc, 4)
    state["trade_count"] = state.get("trade_count", 0) + 1

    # Track P&L percentage
    if equity > 0:
        pnl_pct = (pnl_usdc / equity) * 100
        state["daily_pnl_pct"] = round(
            state.get("daily_pnl_pct", 0.0) + pnl_pct, 4
        )

    if pnl_usdc < 0:
        state["daily_loss_usdc"] += abs(pnl_usdc)

    # Store trade detail
    trades = state.get("trades", [])
    trades.append({
        "pnl_usdc": round(pnl_usdc, 4),
        "pnl_pct": round((pnl_usdc / equity) * 100, 4) if equity > 0 else 0.0,
    })
    state["trades"] = trades[-50:] if isinstance(trades, list) and len(trades) > 50 else trades

    _save_state(state)
    logger.info(
        f"Trade #{state['trade_count']} closed | "
        f"PnL: ${pnl_usdc:+.2f} ({pnl_pct:+.2f}%) | "
        f"Day net: ${state['daily_pnl_usdc']:+.2f} USDC ({state['daily_pnl_pct']:+.2f}%)"
        if equity > 0 else
        f"Trade #{state['trade_count']} closed | "
        f"PnL: ${pnl_usdc:+.2f} | "
        f"Day net: ${state['daily_pnl_usdc']:+.2f} USDC"
    )


def get_daily_pnl() -> dict:
    """
    Return today's trading summary.
    Safe to call from anywhere (Telegram UI, dashboard, etc.).

    Returns:
        dict with keys: date, daily_pnl_usdc, daily_pnl_pct, daily_loss_usdc,
                        trade_count, trades, dry_run_*
    """
    return _get_daily_state()


def get_daily_pnl_pct() -> float:
    """Return today's net P&L as a percentage."""
    state = _get_daily_state()
    return state.get("daily_pnl_pct", 0.0)


# -----------------------------------------------------------
# Dry-Run Simulation Tracking
# -----------------------------------------------------------

def init_dry_run_equity(starting_equity: float):
    """Initialize dry-run simulation equity on first run."""
    state = _get_daily_state()
    if state.get("dry_run_equity", 0.0) <= 0:
        state["dry_run_equity"] = starting_equity
        state["dry_run_starting_equity"] = starting_equity
        _save_state(state)
        logger.info(f"Dry-run equity initialized: ${starting_equity:.2f}")


def record_dry_run_entry(side: str, entry_price: float, size: float):
    """Record a simulated trade entry."""
    state = _get_daily_state()
    state["dry_run_open_trade"] = {
        "side": side,
        "entry": entry_price,
        "size": size,
    }
    _save_state(state)
    logger.info(f"[DRY RUN] Entry recorded: {side} {size} BTC @ ${entry_price:,.2f}")


def record_dry_run_exit(exit_price: float) -> Optional[dict]:
    """
    Record a simulated trade exit, compute P&L.

    Returns:
        dict with pnl_usdc, pnl_pct, or None if no open dry-run trade.
    """
    state = _get_daily_state()
    trade = state.get("dry_run_open_trade")
    if trade is None:
        return None

    side = trade["side"]
    entry = trade["entry"]
    size = trade["size"]

    if side == "BUY":
        pnl_usdc = (exit_price - entry) * size
    else:
        pnl_usdc = (entry - exit_price) * size

    equity = state.get("dry_run_equity", 1000.0)
    pnl_pct = (pnl_usdc / equity) * 100 if equity > 0 else 0.0

    # Update equity
    state["dry_run_equity"] = round(equity + pnl_usdc, 4)
    state["dry_run_pnl_usdc"] = round(
        state.get("dry_run_pnl_usdc", 0.0) + pnl_usdc, 4
    )
    state["dry_run_pnl_pct"] = round(
        state.get("dry_run_pnl_pct", 0.0) + pnl_pct, 4
    )

    # Store trade
    dry_trades = state.get("dry_run_trades", [])
    trade_record = {
        "side": side,
        "entry": round(entry, 2),
        "exit": round(exit_price, 2),
        "size": size,
        "pnl_usdc": round(pnl_usdc, 4),
        "pnl_pct": round(pnl_pct, 4),
    }
    dry_trades.append(trade_record)
    state["dry_run_trades"] = dry_trades[-50:]  # keep last 50
    state["dry_run_open_trade"] = None
    state["trade_count"] = state.get("trade_count", 0) + 1

    _save_state(state)
    logger.info(
        f"[DRY RUN] Exit @ ${exit_price:,.2f} | "
        f"PnL: ${pnl_usdc:+.2f} ({pnl_pct:+.2f}%) | "
        f"Equity: ${state['dry_run_equity']:,.2f}"
    )
    return trade_record


def get_dry_run_stats() -> dict:
    """Return dry-run simulation stats."""
    state = _get_daily_state()
    dry_trades = state.get("dry_run_trades", [])
    wins = sum(1 for t in dry_trades if t.get("pnl_usdc", 0) > 0)
    total = len(dry_trades)
    return {
        "equity": state.get("dry_run_equity", 0.0),
        "starting_equity": state.get("dry_run_starting_equity", 0.0),
        "open_trade": state.get("dry_run_open_trade"),
        "daily_pnl_usdc": state.get("dry_run_pnl_usdc", 0.0),
        "daily_pnl_pct": state.get("dry_run_pnl_pct", 0.0),
        "trade_count": total,
        "win_count": wins,
        "win_rate": (wins / total * 100) if total > 0 else 0.0,
    }


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
# Stop Loss (SL only — exits are structure-based via CHOCH)
# -----------------------------------------------------------

def calculate_sl(
    entry_price: float,
    side: str,          # "BUY" or "SELL"
    df: Optional[pd.DataFrame] = None,
) -> float:
    """
    Calculate stop-loss price (emergency safety net).

    Uses swing-based SL if df is provided, else falls back to percentage.
    TP is no longer used — exits are driven by CHOCH signals.

    Args:
        entry_price: Filled price of the order.
        side: "BUY" (long) or "SELL" (short).
        df: Processed SMC DataFrame with swing_high/swing_low columns.

    Returns:
        stop_loss_price
    """
    from config import cfg  # late import to avoid circular dependency
    sl_pct = float(cfg.stop_loss_pct)

    # Try swing-based SL
    if df is not None and "swing_high" in df.columns and "swing_low" in df.columns:
        recent_lows = df["swing_low"].dropna()
        recent_highs = df["swing_high"].dropna()

        if side == "BUY" and len(recent_lows) > 0:
            swing_sl = recent_lows.iloc[-1]
            if swing_sl > entry_price * (1 - sl_pct * 2):
                logger.debug(f"Swing-based SL: {swing_sl:.2f}")
                return round(swing_sl, 2)

        elif side == "SELL" and len(recent_highs) > 0:
            swing_sl = recent_highs.iloc[-1]
            if swing_sl < entry_price * (1 + sl_pct * 2):
                logger.debug(f"Swing-based SL: {swing_sl:.2f}")
                return round(swing_sl, 2)

    # Fallback: fixed percentage
    if side == "BUY":
        sl = entry_price * (1 - sl_pct)
    else:
        sl = entry_price * (1 + sl_pct)

    logger.debug(f"Pct-based SL: {sl:.2f}")
    return round(sl, 2)


# Keep calculate_sl_tp for backwards compatibility
def calculate_sl_tp(
    entry_price: float,
    side: str,
    df: Optional[pd.DataFrame] = None,
) -> Tuple[float, float]:
    """Backwards-compatible wrapper. TP is set far away since exits are CHOCH-based."""
    sl = calculate_sl(entry_price, side, df)
    # Set TP very far away — actual exit is via CHOCH signal
    if side == "BUY":
        tp = entry_price * 1.50  # 50% — effectively disabled
    else:
        tp = entry_price * 0.50
    return sl, tp


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
