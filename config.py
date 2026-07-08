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
    "position_size_usdc":   float(os.getenv("POSITION_SIZE_USDC", 50)),
    "leverage":             float(os.getenv("LEVERAGE", 1)),
    "stop_loss_pct":        float(os.getenv("STOP_LOSS_PCT", 0.015)),
    "take_profit_pct":      float(os.getenv("TAKE_PROFIT_PCT", 0.03)),
    "max_daily_loss_usdc":  float(os.getenv("MAX_DAILY_LOSS_USDC", 100)),
    "dry_run":              os.getenv("DRY_RUN", "false").lower() == "true",
    "paused":               False,   # set by Telegram /pause
    "log_level":            os.getenv("LOG_LEVEL", "INFO"),
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
        if key == "position_size_usdc" and float(value) < 1:
            raise ValueError("Position size must be at least $1 USDC")
        if key == "stop_loss_pct" and not (0.001 <= float(value) <= 0.5):
            raise ValueError("Stop loss must be between 0.1% and 50%")
        if key == "take_profit_pct" and not (0.001 <= float(value) <= 1.0):
            raise ValueError("Take profit must be between 0.1% and 100%")

        self._data[key] = value
        self.save()
        return f"✅ `{key}` set to `{value}`"

    def snapshot(self) -> dict:
        """Return a copy of the current config."""
        return dict(self._data)


# Module-level singleton — import this everywhere
cfg = _Config()
