"""
dydx_client.py — dYdX v4 API Wrapper
Handles authentication via private key + wallet address,
market data fetching, and order placement.
"""

import asyncio
import logging
import os
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# dYdX v4 client imports
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.node.market import Market

# MAX_CLIENT_ID is not exported in dydx-v4-client==1.1.0 — define it directly
MAX_CLIENT_ID = 2**32 - 1  # max 32-bit unsigned int, used as order client_id
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient as RestIndexerClient
from dydx_v4_client.network import MAINNET, make_testnet
from dydx_v4_client.wallet import Wallet

load_dotenv()
logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# Network config
# -----------------------------------------------------------
NETWORKS = {
    "mainnet": MAINNET,
    "testnet": make_testnet(),
}


class DydxClient:
    """
    Async wrapper around dYdX v4 Python client.
    Authenticates with either:
      • Private key (DYDX_PRIVATE_KEY) + wallet address (DYDX_WALLET_ADDRESS)  ← preferred
      • 24-word mnemonic (DYDX_MNEMONIC)  ← fallback

    The active trading config is always read from config.cfg so that
    Telegram changes take effect on the next poll cycle without restart.
    """

    def __init__(self):
        # Auth credentials — read once at startup from .env
        self._private_key: Optional[str] = os.getenv("DYDX_PRIVATE_KEY")
        self._mnemonic: Optional[str]    = os.getenv("DYDX_MNEMONIC")
        self._wallet_address: Optional[str] = os.getenv("DYDX_WALLET_ADDRESS")

        if not self._private_key and not self._mnemonic:
            raise EnvironmentError(
                "No dYdX credentials found. Set DYDX_PRIVATE_KEY (+ DYDX_WALLET_ADDRESS) "
                "or DYDX_MNEMONIC in your .env file."
            )

        self.node: Optional[NodeClient]        = None
        self.indexer: Optional[RestIndexerClient] = None
        self.wallet: Optional[Wallet]          = None
        self.address: Optional[str]            = None

    # -------------------------------------------------------
    # Live config helpers (read from cfg singleton each time)
    # -------------------------------------------------------
    @property
    def _cfg(self):
        # Late import to avoid circular dependency
        from config import cfg
        return cfg

    @property
    def network_name(self) -> str:
        return self._cfg.network

    @property
    def symbol(self) -> str:
        return self._cfg.symbol

    @property
    def dry_run(self) -> bool:
        return self._cfg.dry_run

    # -------------------------------------------------------
    # CONNECTION
    # -------------------------------------------------------
    async def connect(self):
        """Authenticate and establish connections to dYdX v4."""
        network = NETWORKS.get(self.network_name)
        if network is None:
            raise ValueError(f"Unknown network '{self.network_name}'. Use 'mainnet' or 'testnet'.")

        logger.info(f"Connecting to dYdX {self.network_name}...")

        self.node    = await NodeClient.connect(network.node)
        self.indexer = RestIndexerClient(network.rest_indexer)

        # --- Auth: private key preferred, mnemonic fallback ---
        if self._private_key:
            self.wallet, self.address = await self._auth_private_key(network)
        else:
            self.wallet = await Wallet.from_mnemonic(self.node, self._mnemonic)
            self.address = self.wallet.address

        logger.info(f"✅ Connected | Network: {self.network_name} | Address: {self.address}")

    async def _auth_private_key(self, network):
        """
        Authenticate using a raw secp256k1 private key (hex string).
        Uses cosmpy under the hood (bundled with dydx-v4-client).
        """
        try:
            from cosmpy.crypto.keypairs import Secp256k1
            from dydx_v4_client.wallet import Wallet as V4Wallet

            # Strip 0x prefix if present
            pk_hex = self._private_key.lstrip("0x")
            keypair = Secp256k1(bytes.fromhex(pk_hex))

            # Build wallet from keypair
            wallet = await V4Wallet.from_key(self.node, keypair)

            # Use provided address or derive from wallet
            address = self._wallet_address or wallet.address
            wallet.address = address  # override if user provided one

            logger.info(f"🔑 Authenticated via private key. Address: {address}")
            return wallet, address

        except ImportError:
            raise ImportError(
                "cosmpy is required for private key auth. "
                "It should be bundled with dydx-v4-client. "
                "Try: pip install dydx-v4-client --upgrade"
            )
        except Exception as e:
            raise RuntimeError(f"Private key authentication failed: {e}") from e

    async def reconnect(self):
        """Reconnect — useful after a network switch via Telegram."""
        logger.info("Reconnecting to dYdX...")
        await self.close()
        await self.connect()

    async def close(self):
        """Close all connections gracefully."""
        if self.node:
            await self.node.close()
        self.node = None
        self.indexer = None
        logger.info("Connections closed.")

    # -------------------------------------------------------
    # MARKET DATA
    # -------------------------------------------------------
    async def get_candles(
        self,
        symbol: Optional[str] = None,
        resolution: Optional[str] = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candle data from the dYdX Indexer.
        Returns a DataFrame with columns: open, high, low, close, volume
        sorted oldest-first (for SMC analysis).
        """
        symbol     = symbol     or self.symbol
        resolution = resolution or self._cfg.candle_resolution

        logger.debug(f"Fetching {limit} × {resolution} candles for {symbol}...")

        response = await asyncio.to_thread(
            self.indexer.markets.get_candles,
            market=symbol,
            resolution=resolution,
            limit=limit,
        )

        candles = response.get("candles", [])
        if not candles:
            raise ValueError(f"No candle data returned for {symbol} [{resolution}]")

        df = pd.DataFrame(candles)
        df = df.rename(columns={
            "baseTokenVolume": "volume",
            "startedAt":       "timestamp",
        })

        numeric_cols = ["open", "high", "low", "close", "volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)
        df["timestamp"]  = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()

        logger.debug(f"Got {len(df)} candles | Latest close: ${df['close'].iloc[-1]:,.2f}")
        return df[["open", "high", "low", "close", "volume"]]

    async def get_orderbook(self, symbol: Optional[str] = None) -> dict:
        """Get best bid/ask from the orderbook."""
        symbol = symbol or self.symbol
        ob = await asyncio.to_thread(
            self.indexer.markets.get_orderbook, market=symbol
        )
        best_bid = float(ob["bids"][0]["price"]) if ob.get("bids") else None
        best_ask = float(ob["asks"][0]["price"]) if ob.get("asks") else None
        return {"bid": best_bid, "ask": best_ask}

    # -------------------------------------------------------
    # ACCOUNT & POSITION
    # -------------------------------------------------------
    async def get_account(self) -> dict:
        """Return subaccount info (equity, freeCollateral, etc.)."""
        resp = await asyncio.to_thread(
            self.indexer.account.get_subaccount,
            address=self.address,
            subaccount_number=0,
        )
        return resp.get("subaccount", {})

    async def get_position(self, symbol: Optional[str] = None) -> Optional[dict]:
        """
        Return current open position for symbol, or None if flat.
        Dict keys: side, size, entryPrice, unrealizedPnl, liquidationPrice
        """
        symbol = symbol or self.symbol
        resp = await asyncio.to_thread(
            self.indexer.account.get_subaccount_perpetual_positions,
            address=self.address,
            subaccount_number=0,
            status="OPEN",
        )
        positions = resp.get("positions", [])
        for pos in positions:
            if pos.get("market") == symbol:
                return pos
        return None

    # -------------------------------------------------------
    # ORDER PLACEMENT
    # -------------------------------------------------------
    async def place_market_order(
        self,
        side: str,           # "BUY" or "SELL"
        size: float,         # contract size in BTC
        reduce_only: bool = False,
    ) -> Optional[dict]:
        """
        Place a market order on dYdX v4.
        In dry-run mode, logs the order but does NOT submit it.
        Leverage is read live from cfg.
        """
        logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}Order: {side} {size} BTC "
            f"| reduce_only={reduce_only} | leverage={self._cfg.leverage}x"
        )

        if self.dry_run:
            return {"status": "DRY_RUN", "side": side, "size": size}

        market_info = await asyncio.to_thread(
            self.indexer.markets.get_perpetual_market, self.symbol
        )
        market = Market(market_info["market"])

        order_id = market.order_id(
            address=self.address,
            subaccount_number=0,
            client_id=MAX_CLIENT_ID,
            order_flags=market.order_flags_short_term(),
        )

        current_block  = await self.node.latest_block_height()
        good_til_block = current_block + 10  # valid for ~10 blocks (~1 min)

        order = market.order(
            order_id=order_id,
            order_type="MARKET",
            side=side,
            size=size,
            price=0,           # market order — price ignored
            time_in_force="GTT",
            good_til_block=good_til_block,
            reduce_only=reduce_only,
        )

        tx_hash = await self.node.place_order(self.wallet, order)
        logger.info(f"✅ Order placed | TX: {tx_hash}")
        return {"tx_hash": tx_hash, "side": side, "size": size}

    async def close_position(self, symbol: Optional[str] = None) -> Optional[dict]:
        """Close the current open position entirely (reduce-only market order)."""
        symbol   = symbol or self.symbol
        position = await self.get_position(symbol)

        if position is None:
            logger.info("No open position to close.")
            return None

        pos_side = position.get("side")          # "LONG" or "SHORT"
        pos_size = abs(float(position.get("size", 0)))
        close_side = "SELL" if pos_side == "LONG" else "BUY"

        logger.info(f"Closing {pos_side} position | {pos_size} BTC...")
        return await self.place_market_order(close_side, pos_size, reduce_only=True)
