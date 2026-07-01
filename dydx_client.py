"""
dydx_client.py — dYdX v4 API Wrapper
Handles authentication, market data fetching, and order placement.
"""

import asyncio
import logging
import os
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# dYdX v4 client imports
from dydx_v4_client import NodeClient, IndexerClient
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient as RestIndexerClient
from dydx_v4_client.network import MAINNET, make_testnet
from dydx_v4_client.node.market import Market
from dydx_v4_client.wallet import Wallet
from dydx_v4_client import MAX_CLIENT_ID

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
    Async-compatible wrapper around the dYdX v4 Python client.
    Exposes the methods the trading bot needs:
      - connect()
      - get_candles()
      - get_position()
      - get_account()
      - place_market_order()
      - close_position()
    """

    def __init__(self):
        self.mnemonic: str = os.environ["DYDX_MNEMONIC"]
        self.network_name: str = os.getenv("DYDX_NETWORK", "mainnet")
        self.network = NETWORKS[self.network_name]
        self.symbol: str = os.getenv("TRADE_SYMBOL", "BTC-USD")
        self.dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"

        self.node: Optional[NodeClient] = None
        self.indexer: Optional[RestIndexerClient] = None
        self.wallet: Optional[Wallet] = None
        self.address: Optional[str] = None

    # -------------------------------------------------------
    # CONNECTION
    # -------------------------------------------------------
    async def connect(self):
        """Authenticate and establish connection to dYdX v4."""
        logger.info(f"Connecting to dYdX {self.network_name}...")

        self.node = await NodeClient.connect(self.network.node)
        self.indexer = RestIndexerClient(self.network.rest_indexer)
        self.wallet = await Wallet.from_mnemonic(self.node, self.mnemonic)
        self.address = self.wallet.address

        logger.info(f"✅ Connected. Wallet address: {self.address}")

    async def close(self):
        """Close all connections gracefully."""
        if self.node:
            await self.node.close()
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
        symbol = symbol or self.symbol
        resolution = resolution or os.getenv("CANDLE_RESOLUTION", "15MINS")

        logger.debug(f"Fetching {limit} {resolution} candles for {symbol}...")

        response = await asyncio.to_thread(
            self.indexer.markets.get_candles,
            market=symbol,
            resolution=resolution,
            limit=limit,
        )

        candles = response.get("candles", [])
        if not candles:
            raise ValueError(f"No candle data returned for {symbol}")

        df = pd.DataFrame(candles)
        # Rename columns to match SMC logic expectations
        df = df.rename(columns={
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "baseTokenVolume": "volume",
            "startedAt": "timestamp",
        })

        numeric_cols = ["open", "high", "low", "close", "volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()  # oldest first

        logger.debug(f"Got {len(df)} candles. Latest close: {df['close'].iloc[-1]:.2f}")
        return df[["open", "high", "low", "close", "volume"]]

    async def get_orderbook(self, symbol: Optional[str] = None) -> dict:
        """Get the current best bid/ask."""
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
        """Return subaccount info (USDC equity, free collateral)."""
        resp = await asyncio.to_thread(
            self.indexer.account.get_subaccount,
            address=self.address,
            subaccount_number=0,
        )
        return resp.get("subaccount", {})

    async def get_position(self, symbol: Optional[str] = None) -> Optional[dict]:
        """
        Return current open position for symbol, or None if flat.
        Position dict contains: side, size, entryPrice, unrealizedPnl
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
        side: str,          # "BUY" or "SELL"
        size: float,        # contract size in BTC
        reduce_only: bool = False,
    ) -> Optional[dict]:
        """
        Place a market order on dYdX v4.
        In dry-run mode, logs the order but does NOT submit it.
        """
        action = "LONG" if side == "BUY" else "SHORT"
        logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}Placing {side} market order: "
            f"{size} BTC | reduce_only={reduce_only}"
        )

        if self.dry_run:
            return {"status": "DRY_RUN", "side": side, "size": size}

        market = Market(
            (await asyncio.to_thread(
                self.indexer.markets.get_perpetual_market, self.symbol
            ))["market"]
        )

        order_id = market.order_id(
            address=self.address,
            subaccount_number=0,
            client_id=MAX_CLIENT_ID,
            order_flags=market.order_flags_short_term(),
        )

        current_block = await self.node.latest_block_height()
        good_til_block = current_block + 10  # valid for ~10 blocks (~1 min)

        order = market.order(
            order_id=order_id,
            order_type="MARKET",
            side=side,
            size=size,
            price=0,  # market order
            time_in_force="GTT",
            good_til_block=good_til_block,
            reduce_only=reduce_only,
        )

        tx_hash = await self.node.place_order(self.wallet, order)
        logger.info(f"✅ Order placed. TX hash: {tx_hash}")
        return {"tx_hash": tx_hash, "side": side, "size": size}

    async def close_position(self, symbol: Optional[str] = None) -> Optional[dict]:
        """Close the current open position entirely."""
        symbol = symbol or self.symbol
        position = await self.get_position(symbol)

        if position is None:
            logger.info("No open position to close.")
            return None

        pos_side = position.get("side")  # "LONG" or "SHORT"
        pos_size = abs(float(position.get("size", 0)))

        # To close: send opposite side, reduce_only=True
        close_side = "SELL" if pos_side == "LONG" else "BUY"
        logger.info(f"Closing {pos_side} position of {pos_size} BTC...")
        return await self.place_market_order(close_side, pos_size, reduce_only=True)
