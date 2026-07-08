"""
dydx_client.py — dYdX v4 API Wrapper
Handles authentication via mnemonic or private key,
market data fetching, and order placement.

Verified against dydx-v4-client==1.1.0 source at:
https://github.com/dydxprotocol/v4-clients/tree/main/v4-client-py-v2
"""

import logging
import os
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# dYdX v4 client imports — verified against 1.1.0 PyPI release
from dydx_v4_client import MAX_CLIENT_ID, OrderFlags
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.node.market import Market
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient as RestIndexerClient
from dydx_v4_client.network import make_mainnet, make_testnet
from dydx_v4_client.wallet import Wallet
# NOTE: key_pair module doesn't exist in PyPI 1.1.0 — private key auth
# uses coincurve directly (see _auth_private_key method)

load_dotenv()
logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# Network config
# -----------------------------------------------------------
# make_mainnet() is a partial that needs node_url, rest_indexer, websocket_indexer
# make_testnet() is pre-configured with defaults → call it to get a Network

MAINNET = make_mainnet(
    node_url="dydx-grpc.kingnodes.com",
    rest_indexer="https://indexer.dydx.trade",
    websocket_indexer="wss://indexer.dydx.trade/v4/ws",
)
TESTNET = make_testnet()

NETWORKS = {
    "mainnet": MAINNET,
    "testnet": TESTNET,
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
            self.wallet, self.address = await self._auth_private_key()
        else:
            # Wallet.from_mnemonic requires (node, mnemonic, address)
            self.wallet = await Wallet.from_mnemonic(
                self.node, self._mnemonic, self._wallet_address
            )
            self.address = self.wallet.address

        logger.info(f"✅ Connected | Network: {self.network_name} | Address: {self.address}")

    async def _auth_private_key(self):
        """
        Authenticate using a raw private key (hex string).
        Tries dydx_v4_client.key_pair.KeyPair first (newer versions),
        falls back to coincurve (always installed as a dependency).
        """
        # Strip 0x prefix if present
        pk_hex = self._private_key.lstrip("0x")
        pk_bytes = bytes.fromhex(pk_hex)

        # Try to get KeyPair from dydx_v4_client (newer versions have it)
        key_obj = None
        try:
            from dydx_v4_client.key_pair import KeyPair
            key_obj = KeyPair.from_hex(pk_hex)
            logger.debug("Using dydx_v4_client.key_pair.KeyPair")
        except (ImportError, AttributeError):
            pass

        # Fallback: build a shim using coincurve (installed as dydx dependency)
        if key_obj is None:
            try:
                import hashlib
                import bech32
                from coincurve import PrivateKey as CCPrivateKey
                from Crypto.Hash import RIPEMD160

                cc_key = CCPrivateKey(pk_bytes)
                pub_bytes = cc_key.public_key.format(compressed=True)

                class _KeyShim:
                    """Minimal shim matching the Wallet.key interface."""
                    def __init__(self, priv, pub):
                        self._priv = priv
                        self.public_key_bytes = pub

                    def sign(self, message: bytes) -> bytes:
                        return CCPrivateKey(self._priv).sign(message)

                key_obj = _KeyShim(pk_bytes, pub_bytes)

                # Derive dydx1... address from public key
                sha = hashlib.sha256(pub_bytes).digest()
                ripe = RIPEMD160.new(sha).digest()
                derived_address = bech32.bech32_encode(
                    "dydx", bech32.convertbits(ripe, 8, 5)
                )
                logger.debug(f"Using coincurve shim. Derived: {derived_address}")
            except Exception as e:
                raise RuntimeError(
                    f"Private key auth failed — could not load key_pair or coincurve: {e}\n"
                    f"Consider using DYDX_MNEMONIC instead."
                ) from e

        # Use user-provided address, or the one we derived
        address = self._wallet_address
        if not address:
            # If we derived it above (coincurve path), use that
            if 'derived_address' in dir():
                address = derived_address
            else:
                # KeyPair path — derive from a temp wallet
                temp = Wallet(key=key_obj, account_number=0, sequence=0)
                address = temp.address

        # Get actual account info from chain
        try:
            account = await self.node.get_account(address)
            wallet = Wallet(
                key=key_obj,
                account_number=account.account_number,
                sequence=account.sequence,
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to get account info for {address}: {e}\n"
                f"Make sure DYDX_WALLET_ADDRESS is correct and the account exists on {self.network_name}."
            ) from e

        logger.info(f"🔑 Authenticated via private key. Address: {address}")
        return wallet, address

    async def reconnect(self):
        """Reconnect — useful after a network switch via Telegram."""
        logger.info("Reconnecting to dYdX...")
        await self.close()
        await self.connect()

    async def close(self):
        """Close all connections gracefully."""
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

        response = await self.indexer.markets.get_perpetual_market_candles(
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
        ob = await self.indexer.markets.get_perpetual_market_orderbook(market=symbol)
        best_bid = float(ob["bids"][0]["price"]) if ob.get("bids") else None
        best_ask = float(ob["asks"][0]["price"]) if ob.get("asks") else None
        return {"bid": best_bid, "ask": best_ask}

    # -------------------------------------------------------
    # ACCOUNT & POSITION
    # -------------------------------------------------------
    async def get_account(self) -> dict:
        """Return subaccount info (equity, freeCollateral, etc.)."""
        resp = await self.indexer.account.get_subaccount(
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
        resp = await self.indexer.account.get_subaccount_perpetual_positions(
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

        markets_resp = await self.indexer.markets.get_perpetual_markets(market=self.symbol)
        market_info = markets_resp.get("markets", {}).get(self.symbol, {})
        market = Market(market_info)

        order_id = market.order_id(
            address=self.address,
            subaccount_number=0,
            client_id=MAX_CLIENT_ID,
            order_flags=OrderFlags.SHORT_TERM,
        )

        current_block  = await self.node.latest_block_height()
        good_til_block = current_block + 10  # valid for ~10 blocks (~1 min)

        # Map string side to protobuf enum
        from v4_proto.dydxprotocol.clob.order_pb2 import Order
        order_side = Order.SIDE_BUY if side == "BUY" else Order.SIDE_SELL

        order_obj = market.order(
            order_id=order_id,
            order_type="MARKET",
            side=order_side,
            size=size,
            price=0,           # market order — price ignored
            time_in_force=Order.TIME_IN_FORCE_IOC,
            good_til_block=good_til_block,
            reduce_only=reduce_only,
        )

        tx_hash = await self.node.place_order(self.wallet, order_obj)
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
