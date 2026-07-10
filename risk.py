"""
risk.py — Minimal Risk Layer

Position sizing, daily P&L tracking, and dry-run simulation.
All the complex trailing stop / cooldown / regime stuff is gone.
"""

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

STATE_FILE = Path("state.json")


# -----------------------------------------------------------
# State Persistence
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
        # Dry-run
        "dry_run_equity": 0.0,
        "dry_run_starting_equity": 0.0,
        "dry_run_open_trade": None,
        "dry_run_pnl_usdc": 0.0,
        "dry_run_pnl_pct": 0.0,
        "dry_run_trades": [],
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
        }
        _save_state(state)
    return state


# -----------------------------------------------------------
# Trade P&L Tracking
# -----------------------------------------------------------

def record_trade_pnl(pnl_usdc: float, equity: float = 0.0):
    """Record a closed trade's P&L."""
    state = _get_daily_state()
    state["daily_pnl_usdc"] = round(state.get("daily_pnl_usdc", 0.0) + pnl_usdc, 4)
    state["trade_count"] = state.get("trade_count", 0) + 1

    pnl_pct = 0.0
    if equity > 0:
        pnl_pct = (pnl_usdc / equity) * 100
        state["daily_pnl_pct"] = round(state.get("daily_pnl_pct", 0.0) + pnl_pct, 4)

    if pnl_usdc < 0:
        state["daily_loss_usdc"] += abs(pnl_usdc)

    trades = state.get("trades", [])
    trades.append({"pnl_usdc": round(pnl_usdc, 4), "pnl_pct": round(pnl_pct, 4)})
    state["trades"] = trades[-50:]
    _save_state(state)

    logger.info(
        f"Trade #{state['trade_count']} | PnL: ${pnl_usdc:+.2f} ({pnl_pct:+.2f}%) | "
        f"Day net: ${state['daily_pnl_usdc']:+.2f}"
    )


def get_daily_pnl() -> dict:
    """Return today's trading summary."""
    return _get_daily_state()


# -----------------------------------------------------------
# Daily Loss Guard
# -----------------------------------------------------------

def is_daily_loss_limit_hit() -> bool:
    from config import cfg
    max_loss = float(cfg.max_daily_loss_usdc)
    state = _get_daily_state()
    loss = state.get("daily_loss_usdc", 0.0)
    if loss >= max_loss:
        logger.warning(f"🛑 Daily loss limit: ${loss:.2f} >= ${max_loss:.2f}")
        return True
    return False


# -----------------------------------------------------------
# Position Sizing
# -----------------------------------------------------------

def calculate_position_size(btc_price: float, equity_usdc: float) -> float:
    """Equity fraction + leverage → BTC size. Step = 0.0001 BTC."""
    from config import cfg
    pct = float(cfg.position_size_pct)
    leverage = float(cfg.leverage)
    min_step = 0.0001

    collateral = equity_usdc * pct
    size_btc = (collateral * leverage) / btc_price
    size_btc = round(size_btc - (size_btc % min_step), 4)

    if size_btc < min_step:
        raise ValueError(
            f"Size {size_btc} BTC below min {min_step}. "
            f"Equity ${equity_usdc:.2f} | {pct*100:.0f}% | {leverage}x"
        )
    return size_btc


# -----------------------------------------------------------
# Emergency SL
# -----------------------------------------------------------

def calculate_sl(entry_price: float, side: str) -> float:
    """Simple percentage-based emergency stop loss."""
    from config import cfg
    sl_pct = float(cfg.stop_loss_pct)
    if side == "BUY":
        return round(entry_price * (1 - sl_pct), 2)
    else:
        return round(entry_price * (1 + sl_pct), 2)


# -----------------------------------------------------------
# Entry Guard
# -----------------------------------------------------------

def should_enter(signal: int, current_position: Optional[dict]) -> bool:
    if signal == 0:
        return False
    if current_position is None:
        return True
    pos_side = current_position.get("side", "")
    if signal == 1 and pos_side == "LONG":
        return False
    if signal == -1 and pos_side == "SHORT":
        return False
    return True


# -----------------------------------------------------------
# Dry-Run Simulation
# -----------------------------------------------------------

def init_dry_run_equity(starting_equity: float):
    state = _get_daily_state()
    if state.get("dry_run_equity", 0.0) <= 0:
        state["dry_run_equity"] = starting_equity
        state["dry_run_starting_equity"] = starting_equity
        _save_state(state)
        logger.info(f"Dry-run equity: ${starting_equity:.2f}")


def record_dry_run_entry(side: str, entry_price: float, size: float):
    state = _get_daily_state()
    state["dry_run_open_trade"] = {"side": side, "entry": entry_price, "size": size}
    _save_state(state)
    logger.info(f"[DRY] Entry: {side} {size} BTC @ ${entry_price:,.2f}")


def record_dry_run_exit(exit_price: float) -> Optional[dict]:
    state = _get_daily_state()
    trade = state.get("dry_run_open_trade")
    if trade is None:
        return None

    side, entry, size = trade["side"], trade["entry"], trade["size"]
    pnl = (exit_price - entry) * size if side == "BUY" else (entry - exit_price) * size
    equity = state.get("dry_run_equity", 1000.0)
    pnl_pct = (pnl / equity) * 100 if equity > 0 else 0.0

    state["dry_run_equity"] = round(equity + pnl, 4)
    state["dry_run_pnl_usdc"] = round(state.get("dry_run_pnl_usdc", 0.0) + pnl, 4)
    state["dry_run_pnl_pct"] = round(state.get("dry_run_pnl_pct", 0.0) + pnl_pct, 4)

    record = {
        "side": side, "entry": round(entry, 2), "exit": round(exit_price, 2),
        "size": size, "pnl_usdc": round(pnl, 4), "pnl_pct": round(pnl_pct, 4),
    }
    dry_trades = state.get("dry_run_trades", [])
    dry_trades.append(record)
    state["dry_run_trades"] = dry_trades[-50:]
    state["dry_run_open_trade"] = None
    state["trade_count"] = state.get("trade_count", 0) + 1
    _save_state(state)

    logger.info(
        f"[DRY] Exit @ ${exit_price:,.2f} | PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%) | "
        f"Equity: ${state['dry_run_equity']:,.2f}"
    )
    return record


def get_dry_run_stats() -> dict:
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
