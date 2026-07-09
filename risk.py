"""
risk.py — Risk Management Layer
Handles position sizing, stop-loss (ATR-based trailing), daily loss limits,
cooldown logic, and dry-run P&L tracking.
"""

import json
import logging
import os
import time
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
        "daily_pnl_usdc": 0.0,
        "daily_pnl_pct": 0.0,
        "daily_loss_usdc": 0.0,
        "trade_count": 0,
        "trades": [],
        # Dry-run simulation state
        "dry_run_equity": 0.0,
        "dry_run_starting_equity": 0.0,
        "dry_run_open_trade": None,
        "dry_run_pnl_usdc": 0.0,
        "dry_run_pnl_pct": 0.0,
        "dry_run_trades": [],
        # Cooldown & trailing stop state
        "last_loss_time": 0,
        "trailing_sl": 0.0,
        "entry_candle_count": 0,
    }


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_daily_state() -> dict:
    state = _load_state()
    if state.get("date") != str(date.today()):
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
            "last_loss_time": 0,
            "trailing_sl": 0.0,
            "entry_candle_count": 0,
        }
        _save_state(state)
    return state


def record_trade_pnl(pnl_usdc: float, equity: float = 0.0):
    """Call this after a trade closes to track daily net P&L and loss limit."""
    state = _get_daily_state()
    state["daily_pnl_usdc"] = round(state.get("daily_pnl_usdc", 0.0) + pnl_usdc, 4)
    state["trade_count"] = state.get("trade_count", 0) + 1

    pnl_pct = 0.0
    if equity > 0:
        pnl_pct = (pnl_usdc / equity) * 100
        state["daily_pnl_pct"] = round(
            state.get("daily_pnl_pct", 0.0) + pnl_pct, 4
        )

    if pnl_usdc < 0:
        state["daily_loss_usdc"] += abs(pnl_usdc)
        state["last_loss_time"] = time.time()

    trades = state.get("trades", [])
    trades.append({
        "pnl_usdc": round(pnl_usdc, 4),
        "pnl_pct": round(pnl_pct, 4),
    })
    state["trades"] = trades[-50:] if len(trades) > 50 else trades

    _save_state(state)
    logger.info(
        f"Trade #{state['trade_count']} closed | "
        f"PnL: ${pnl_usdc:+.2f} ({pnl_pct:+.2f}%) | "
        f"Day net: ${state['daily_pnl_usdc']:+.2f} USDC ({state['daily_pnl_pct']:+.2f}%)"
    )


def get_daily_pnl() -> dict:
    """Return today's trading summary."""
    return _get_daily_state()


def get_daily_pnl_pct() -> float:
    """Return today's net P&L as a percentage."""
    state = _get_daily_state()
    return state.get("daily_pnl_pct", 0.0)


# -----------------------------------------------------------
# Cooldown Logic
# -----------------------------------------------------------

def is_cooldown_active(poll_interval: int) -> bool:
    """
    Returns True if we're still within cooldown period after a loss.
    Cooldown = cooldown_candles × poll_interval seconds.
    """
    from config import cfg
    cooldown_candles = int(cfg.cooldown_candles)
    if cooldown_candles <= 0:
        return False

    state = _get_daily_state()
    last_loss = state.get("last_loss_time", 0)
    if last_loss <= 0:
        return False

    cooldown_seconds = cooldown_candles * poll_interval
    elapsed = time.time() - last_loss

    if elapsed < cooldown_seconds:
        remaining = cooldown_seconds - elapsed
        logger.debug(f"Cooldown active: {remaining:.0f}s remaining ({cooldown_candles} candles)")
        return True
    return False


# -----------------------------------------------------------
# Trailing Stop Management
# -----------------------------------------------------------

def update_trailing_stop(
    current_price: float,
    side: str,
    entry_price: float,
    atr: float,
    current_trailing_sl: float,
) -> float:
    """
    Compute new trailing stop. Ratchets in favor of profit only.
    As profit grows, the trailing stop tightens (ATR multiplier decreases).

    Returns the new trailing stop price.
    """
    from config import cfg
    base_mult = float(cfg.trailing_atr_mult)

    if atr <= 0:
        return current_trailing_sl

    if side == "BUY":
        profit_pct = (current_price - entry_price) / entry_price
        # Tighten trailing stop as profit grows
        if profit_pct > 0.005:  # > 0.5% profit
            mult = max(base_mult * 0.7, 0.5)  # tighten
        elif profit_pct > 0.01:
            mult = max(base_mult * 0.5, 0.3)  # very tight
        else:
            mult = base_mult

        new_sl = current_price - mult * atr
        # Only ratchet up
        return max(new_sl, current_trailing_sl) if current_trailing_sl > 0 else new_sl

    else:  # SELL
        profit_pct = (entry_price - current_price) / entry_price
        if profit_pct > 0.005:
            mult = max(base_mult * 0.7, 0.5)
        elif profit_pct > 0.01:
            mult = max(base_mult * 0.5, 0.3)
        else:
            mult = base_mult

        new_sl = current_price + mult * atr
        # Only ratchet down
        return min(new_sl, current_trailing_sl) if current_trailing_sl > 0 else new_sl


def save_trailing_sl(sl: float):
    """Persist trailing SL to state file."""
    state = _get_daily_state()
    state["trailing_sl"] = round(sl, 2)
    _save_state(state)


def get_trailing_sl() -> float:
    """Get saved trailing SL."""
    state = _get_daily_state()
    return state.get("trailing_sl", 0.0)


def increment_hold_counter() -> int:
    """Increment candle hold counter. Returns current count."""
    state = _get_daily_state()
    count = state.get("entry_candle_count", 0) + 1
    state["entry_candle_count"] = count
    _save_state(state)
    return count


def reset_hold_counter():
    """Reset hold counter (called when position closes)."""
    state = _get_daily_state()
    state["entry_candle_count"] = 0
    state["trailing_sl"] = 0.0
    _save_state(state)


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
    """Record a simulated trade exit, compute P&L."""
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

    state["dry_run_equity"] = round(equity + pnl_usdc, 4)
    state["dry_run_pnl_usdc"] = round(
        state.get("dry_run_pnl_usdc", 0.0) + pnl_usdc, 4
    )
    state["dry_run_pnl_pct"] = round(
        state.get("dry_run_pnl_pct", 0.0) + pnl_pct, 4
    )

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
    state["dry_run_trades"] = dry_trades[-50:]
    state["dry_run_open_trade"] = None
    state["trade_count"] = state.get("trade_count", 0) + 1

    if pnl_usdc < 0:
        state["last_loss_time"] = time.time()

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
    dYdX BTC-USD contract step size = 0.0001 BTC.
    """
    from config import cfg
    pct      = float(cfg.position_size_pct)
    leverage = float(cfg.leverage)
    min_step = 0.0001

    collateral_usdc = equity_usdc * pct
    size_btc = (collateral_usdc * leverage) / btc_price
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
# Stop Loss (ATR-based + emergency percentage)
# -----------------------------------------------------------

def calculate_sl(
    entry_price: float,
    side: str,
    atr: float = 0.0,
    df: Optional[pd.DataFrame] = None,
) -> float:
    """
    Calculate initial stop-loss price.
    Uses ATR-based stop if atr > 0, else falls back to percentage.
    """
    from config import cfg
    sl_pct = float(cfg.stop_loss_pct)
    trailing_mult = float(cfg.trailing_atr_mult)

    # ATR-based SL
    if atr > 0:
        if side == "BUY":
            sl = entry_price - trailing_mult * atr
        else:
            sl = entry_price + trailing_mult * atr

        # Clamp: never wider than 2× the percentage SL
        max_sl_dist = entry_price * sl_pct * 2
        if side == "BUY":
            sl = max(sl, entry_price - max_sl_dist)
        else:
            sl = min(sl, entry_price + max_sl_dist)

        logger.debug(f"ATR-based SL: {sl:.2f} (ATR={atr:.2f}, mult={trailing_mult})")
        return round(sl, 2)

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
    """Backwards-compatible wrapper."""
    sl = calculate_sl(entry_price, side, df=df)
    if side == "BUY":
        tp = entry_price * 1.50
    else:
        tp = entry_price * 0.50
    return sl, tp


# -----------------------------------------------------------
# Daily Loss Guard
# -----------------------------------------------------------

def is_daily_loss_limit_hit() -> bool:
    """Returns True if today's loss has exceeded MAX_DAILY_LOSS_USDC."""
    from config import cfg
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
    """
    if signal == 0:
        return False

    if current_position is None:
        return True

    pos_side = current_position.get("side", "")
    if signal == 1 and pos_side == "LONG":
        logger.debug("Already LONG — skipping BUY signal.")
        return False
    if signal == -1 and pos_side == "SHORT":
        logger.debug("Already SHORT — skipping SELL signal.")
        return False

    return True
