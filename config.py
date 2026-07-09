"""
config.py — Shared Runtime Configuration
A singleton that holds all live-tuneable settings.
Both the trading loop and Telegram bot read/write this.
Settings are persisted to config.json so they survive restarts.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

CONFIG_FILE = Path("runtime_config.json")

# -----------------------------------------------------------
# Default values (all overridable from .env or via Telegram)
# -----------------------------------------------------------
_DEFAULTS = {
    "network":              os.getenv("DYDX_NETWORK", "mainnet"),
    "symbol":               os.getenv("TRADE_SYMBOL", "BTC-USD"),
    "candle_resolution":    os.getenv("CANDLE_RESOLUTION", "1MIN"),
    "candle_limit":         int(os.getenv("CANDLE_LIMIT", 100)),
    "poll_interval":        int(os.getenv("POLL_INTERVAL_SECONDS", 60)),
    "position_size_pct":    float(os.getenv("POSITION_SIZE_PCT", 0.10)),
    "leverage":             float(os.getenv("LEVERAGE", 1)),
    "stop_loss_pct":        float(os.getenv("STOP_LOSS_PCT", 0.015)),
    "take_profit_pct":      float(os.getenv("TAKE_PROFIT_PCT", 0.03)),
    "max_daily_loss_usdc":  float(os.getenv("MAX_DAILY_LOSS_USDC", 100)),
    "dry_run":              os.getenv("DRY_RUN", "false").lower() == "true",
    "paused":               False,
    "log_level":            os.getenv("LOG_LEVEL", "INFO"),
    # ── SuperTrend Sniper Parameters ──
    "st_atr_period":        int(os.getenv("ST_ATR_PERIOD", 10)),
    "st_multiplier":        float(os.getenv("ST_MULTIPLIER", 3.0)),
    "st_use_true_atr":      os.getenv("ST_USE_TRUE_ATR", "true").lower() == "true",
    "trailing_atr_mult":    float(os.getenv("TRAILING_ATR_MULT", 1.5)),
    "max_hold_candles":     int(os.getenv("MAX_HOLD_CANDLES", 60)),
    "partial_tp_pct":       float(os.getenv("PARTIAL_TP_PCT", 0.5)),
    "cooldown_candles":     int(os.getenv("COOLDOWN_CANDLES", 1)),
    "fee_filter_enabled":   os.getenv("FEE_FILTER_ENABLED", "true").lower() == "true",
    "estimated_fee_pct":    float(os.getenv("ESTIMATED_FEE_PCT", 0.05)),
    # Dry-run simulation
    "dry_run_equity":       float(os.getenv("DRY_RUN_EQUITY", 1000.0)),
}

# Valid choices for constrained fields
VALID_RESOLUTIONS = ["1MIN", "5MINS", "15MINS", "30MINS", "1HOUR", "4HOURS", "1DAY"]
VALID_NETWORKS    = ["mainnet", "testnet"]


class _Config:
    """
    Thread/async-safe runtime config backed by a JSON file.
    Access via the module-level `cfg` singleton.
    """

    def __init__(self):
        self._data: dict = {}
        self._load()

    # -------------------------------------------------------
    # Persistence
    # -------------------------------------------------------
    def _load(self):
        """Load from saved JSON, fall back to defaults."""
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text())
                # Remove old Hydra/SMC keys that no longer exist
                old_keys = {"min_bos_count", "swing_length", "supertrend_atr_period", "supertrend_multiplier",
                            "signal_threshold", "ema_fast", "rsi_period", "bb_period", "adaptive_threshold"}
                for k in old_keys:
                    saved.pop(k, None)
                self._data = {**_DEFAULTS, **saved}
                logger.debug("Loaded runtime config from file.")
                return
            except Exception as e:
                logger.warning(f"Could not load config file: {e} — using defaults.")
        self._data = dict(_DEFAULTS)

    def save(self):
        """Persist current config to disk."""
        try:
            CONFIG_FILE.write_text(json.dumps(self._data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    # -------------------------------------------------------
    # Attribute-style access
    # -------------------------------------------------------
    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(f"Config has no field '{key}'")

    def set(self, key: str, value: Any) -> str:
        """
        Update a config field, validate it, save, and return
        a human-readable confirmation string.
        Raises ValueError if the value is invalid.
        """
        if key not in _DEFAULTS:
            raise ValueError(f"Unknown setting '{key}'")

        # Type-cast to match the default type
        expected_type = type(_DEFAULTS[key])
        try:
            if expected_type == bool:
                if isinstance(value, str):
                    value = value.lower() in ("true", "1", "yes", "on")
                else:
                    value = bool(value)
            else:
                value = expected_type(value)
        except (ValueError, TypeError):
            raise ValueError(f"'{value}' is not a valid {expected_type.__name__} for '{key}'")

        # Domain validation
        if key == "network" and value not in VALID_NETWORKS:
            raise ValueError(f"Network must be one of: {VALID_NETWORKS}")
        if key == "candle_resolution" and value not in VALID_RESOLUTIONS:
            raise ValueError(f"Resolution must be one of: {VALID_RESOLUTIONS}")
        if key == "leverage" and not (1 <= float(value) <= 20):
            raise ValueError("Leverage must be between 1 and 20")
        if key == "position_size_pct" and not (0.01 <= float(value) <= 1.0):
            raise ValueError("Position size must be between 1% and 100% of equity")
        if key == "stop_loss_pct" and not (0.001 <= float(value) <= 0.5):
            raise ValueError("Stop loss must be between 0.1% and 50%")
        if key == "take_profit_pct" and not (0.001 <= float(value) <= 1.0):
            raise ValueError("Take profit must be between 0.1% and 100%")
        if key == "st_atr_period" and not (5 <= int(value) <= 50):
            raise ValueError("SuperTrend ATR period must be between 5 and 50")
        if key == "st_multiplier" and not (0.5 <= float(value) <= 10.0):
            raise ValueError("SuperTrend multiplier must be between 0.5 and 10.0")
        if key == "trailing_atr_mult" and not (0.5 <= float(value) <= 5.0):
            raise ValueError("Trailing ATR multiplier must be between 0.5 and 5.0")
        if key == "max_hold_candles" and not (3 <= int(value) <= 500):
            raise ValueError("Max hold candles must be between 3 and 500")
        if key == "partial_tp_pct" and not (0.1 <= float(value) <= 0.9):
            raise ValueError("Partial TP % must be between 10% and 90%")
        if key == "cooldown_candles" and not (0 <= int(value) <= 20):
            raise ValueError("Cooldown candles must be between 0 and 20")
        if key == "estimated_fee_pct" and not (0.001 <= float(value) <= 1.0):
            raise ValueError("Estimated fee must be between 0.001% and 1.0%")
        if key == "dry_run_equity" and not (10.0 <= float(value) <= 1000000.0):
            raise ValueError("Dry-run equity must be between $10 and $1,000,000")

        self._data[key] = value
        self.save()
        return f"✅ `{key}` set to `{value}`"

    def snapshot(self) -> dict:
        """Return a copy of the current config."""
        return dict(self._data)


# Module-level singleton — import this everywhere
cfg = _Config()
