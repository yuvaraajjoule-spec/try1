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
    "position_size_pct":    float(os.getenv("POSITION_SIZE_PCT", 0.10)),  # fraction of equity, e.g. 0.10 = 10%
    "leverage":             float(os.getenv("LEVERAGE", 1)),
    "stop_loss_pct":        float(os.getenv("STOP_LOSS_PCT", 0.015)),
    "take_profit_pct":      float(os.getenv("TAKE_PROFIT_PCT", 0.03)),
    "max_daily_loss_usdc":  float(os.getenv("MAX_DAILY_LOSS_USDC", 100)),
    "dry_run":              os.getenv("DRY_RUN", "false").lower() == "true",
    "paused":               False,   # set by Telegram /pause
    "log_level":            os.getenv("LOG_LEVEL", "INFO"),
    # SMC Strategy Parameters
    "min_bos_count":        int(os.getenv("MIN_BOS_COUNT", 2)),           # min BOS events before CHOCH triggers signal
    "swing_length":         int(os.getenv("SWING_LENGTH", 5)),            # bars lookback for swing detection
    "supertrend_atr_period": int(os.getenv("SUPERTREND_ATR_PERIOD", 10)), # ATR period for SuperTrend
    "supertrend_multiplier": float(os.getenv("SUPERTREND_MULTIPLIER", 3.0)), # ATR multiplier for SuperTrend
    # Dry-run simulation
    "dry_run_equity":       float(os.getenv("DRY_RUN_EQUITY", 1000.0)),   # simulated starting equity for dry-run
}

# Valid choices for constrained fields
VALID_RESOLUTIONS = ["1MIN", "5MINS", "15MINS", "30MINS", "1HOUR", "4HOURS", "1DAY"]  # Note: 1MIN (no S) — dYdX API rejects '1MINS'
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
        if key == "min_bos_count" and not (1 <= int(value) <= 10):
            raise ValueError("Min BOS count must be between 1 and 10")
        if key == "swing_length" and not (2 <= int(value) <= 50):
            raise ValueError("Swing length must be between 2 and 50")
        if key == "supertrend_atr_period" and not (1 <= int(value) <= 50):
            raise ValueError("SuperTrend ATR period must be between 1 and 50")
        if key == "supertrend_multiplier" and not (0.5 <= float(value) <= 10.0):
            raise ValueError("SuperTrend multiplier must be between 0.5 and 10.0")
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
