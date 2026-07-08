"""
logic.py — SMC + SuperTrend Strategy Engine
Implements Smart Money Concepts (BOS/CHOCH state machine, Order Blocks,
Fair Value Gaps, Premium/Discount Zones) with SuperTrend confirmation.

Signal flow:
  1. Detect swing highs/lows
  2. Track BOS (Break of Structure) events — need ≥ min_bos_count
  3. Detect CHOCH (Change of Character) — trend reversal
  4. Confirm direction with SuperTrend
  5. Optional confluence: Order Block / FVG / Premium-Discount zone
  → Output: BUY / SELL / HOLD + exit signals

Ported from LuxAlgo Smart Money Concepts (PineScript v5)
and SuperTrend indicator (PineScript v4).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BULL = 1
BEAR = -1


# ───────────────────────────────────────────────────────────
# Data Classes
# ───────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    """A detected swing high or low."""
    index: int
    price: float
    is_high: bool  # True = swing high, False = swing low


@dataclass
class StructureEvent:
    """A BOS or CHOCH event."""
    index: int
    event_type: str   # "BOS" or "CHOCH"
    direction: int    # BULL (+1) or BEAR (-1)
    price: float      # the swing level that was broken


@dataclass
class OrderBlock:
    """An order block zone."""
    index: int
    high: float
    low: float
    bias: int         # BULL or BEAR
    mitigated: bool = False


@dataclass
class FairValueGap:
    """A fair value gap (3-candle imbalance)."""
    index: int
    top: float
    bottom: float
    bias: int         # BULL or BEAR
    filled: bool = False


@dataclass
class SignalResult:
    """Rich signal output from the strategy engine."""
    signal: int           # 1=BUY, -1=SELL, 0=HOLD
    exit_signal: bool     # True if current position should be closed
    exit_reason: str      # "choch_reversal", "none"
    smc_trend: int        # current SMC trend direction
    supertrend_dir: int   # current SuperTrend direction
    bos_count: int        # consecutive BOS count in current trend
    last_event: str       # description of latest structure event
    confluence: List[str] # list of confirming factors


# ───────────────────────────────────────────────────────────
# SuperTrend Indicator (ported from PineScript v4)
# ───────────────────────────────────────────────────────────

def compute_supertrend(
    df: pd.DataFrame,
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    Compute SuperTrend indicator.

    Returns df with added columns:
      - supertrend_dir: +1 (bullish) or -1 (bearish)
      - supertrend_upper: upper band
      - supertrend_lower: lower band
      - supertrend_buy: True on bullish flip
      - supertrend_sell: True on bearish flip
    """
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)

    # ATR calculation
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    tr[0] = high[0] - low[0]

    atr = np.zeros(n)
    # SMA for first atr_period bars, then rolling
    if n >= atr_period:
        atr[atr_period - 1] = np.mean(tr[:atr_period])
        for i in range(atr_period, n):
            atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period
    else:
        atr[:] = np.mean(tr)

    src = (high + low) / 2.0  # hl2

    # Upper and lower bands
    up = np.zeros(n)
    dn = np.zeros(n)
    trend = np.ones(n, dtype=int)

    up[0] = src[0] - multiplier * max(atr[0], 0.0001)
    dn[0] = src[0] + multiplier * max(atr[0], 0.0001)

    for i in range(1, n):
        a = max(atr[i], 0.0001)
        up_val = src[i] - multiplier * a
        dn_val = src[i] + multiplier * a

        # Ratchet up/dn
        up[i] = max(up_val, up[i - 1]) if close[i - 1] > up[i - 1] else up_val
        dn[i] = min(dn_val, dn[i - 1]) if close[i - 1] < dn[i - 1] else dn_val

        # Trend
        prev_trend = trend[i - 1]
        if prev_trend == -1 and close[i] > dn[i - 1]:
            trend[i] = 1
        elif prev_trend == 1 and close[i] < up[i - 1]:
            trend[i] = -1
        else:
            trend[i] = prev_trend

    buy_signal = np.zeros(n, dtype=bool)
    sell_signal = np.zeros(n, dtype=bool)
    for i in range(1, n):
        buy_signal[i] = (trend[i] == 1) and (trend[i - 1] == -1)
        sell_signal[i] = (trend[i] == -1) and (trend[i - 1] == 1)

    df = df.copy()
    df["supertrend_dir"] = trend
    df["supertrend_upper"] = dn  # upper band (resistance in downtrend)
    df["supertrend_lower"] = up  # lower band (support in uptrend)
    df["supertrend_buy"] = buy_signal
    df["supertrend_sell"] = sell_signal

    return df


# ───────────────────────────────────────────────────────────
# SMC Engine — Stateful Analysis
# ───────────────────────────────────────────────────────────

class SMCEngine:
    """
    Smart Money Concepts engine.

    Tracks swing structure, detects BOS/CHOCH events,
    identifies order blocks and fair value gaps.
    Maintains a state machine for signal generation.
    """

    def __init__(
        self,
        swing_length: int = 5,
        min_bos_count: int = 2,
    ):
        self.swing_length = swing_length
        self.min_bos_count = min_bos_count

        # State
        self.swing_highs: List[SwingPoint] = []
        self.swing_lows: List[SwingPoint] = []
        self.structure_events: List[StructureEvent] = []
        self.order_blocks: List[OrderBlock] = []
        self.fair_value_gaps: List[FairValueGap] = []

        self.current_trend: int = 0     # BULL or BEAR or 0 (unknown)
        self.bos_count: int = 0         # consecutive BOS in current trend
        self.last_swing_high: Optional[SwingPoint] = None
        self.last_swing_low: Optional[SwingPoint] = None
        self.high_crossed: bool = False
        self.low_crossed: bool = False

        # Trailing extremes for premium/discount zones
        self.trailing_high: float = 0.0
        self.trailing_low: float = float("inf")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """Run full SMC analysis on OHLCV dataframe."""
        df = df.copy()
        n = len(df)
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        op = df["open"].values

        # Initialize output columns
        df["smc_trend"] = 0
        df["bos"] = 0
        df["choch"] = 0
        df["swing_high"] = np.nan
        df["swing_low"] = np.nan
        df["order_block"] = 0
        df["fvg"] = 0
        df["bos_count"] = 0

        length = self.swing_length

        # ── 1. Detect swing points ──────────────────────
        for i in range(length, n - length):
            window_high = high[max(0, i - length):i + length + 1]
            window_low = low[max(0, i - length):i + length + 1]

            if high[i] == np.max(window_high):
                sp = SwingPoint(index=i, price=high[i], is_high=True)
                self.swing_highs.append(sp)
                df.iloc[i, df.columns.get_loc("swing_high")] = high[i]

            if low[i] == np.min(window_low):
                sp = SwingPoint(index=i, price=low[i], is_high=False)
                self.swing_lows.append(sp)
                df.iloc[i, df.columns.get_loc("swing_low")] = low[i]

        # ── 2. Detect BOS / CHOCH ───────────────────────
        for i in range(1, n):
            c = close[i]

            # Get the most recent swing high/low BEFORE this bar
            recent_sh = [s for s in self.swing_highs if s.index < i]
            recent_sl = [s for s in self.swing_lows if s.index < i]

            if not recent_sh or not recent_sl:
                df.iloc[i, df.columns.get_loc("smc_trend")] = self.current_trend
                df.iloc[i, df.columns.get_loc("bos_count")] = self.bos_count
                continue

            last_high = recent_sh[-1]
            last_low = recent_sl[-1]

            # Update tracking references
            if self.last_swing_high is None or last_high.index != self.last_swing_high.index:
                self.last_swing_high = last_high
                self.high_crossed = False
            if self.last_swing_low is None or last_low.index != self.last_swing_low.index:
                self.last_swing_low = last_low
                self.low_crossed = False

            # ── Bullish break (close > last swing high) ──
            if c > last_high.price and not self.high_crossed:
                self.high_crossed = True
                if self.current_trend == BEAR:
                    # CHOCH — trend reversal from bear to bull
                    event = StructureEvent(i, "CHOCH", BULL, last_high.price)
                    self.structure_events.append(event)
                    df.iloc[i, df.columns.get_loc("choch")] = BULL
                    self.bos_count = 0
                    self._store_order_block(df, i, last_high, BULL, op, close)
                else:
                    # BOS — continuation
                    event = StructureEvent(i, "BOS", BULL, last_high.price)
                    self.structure_events.append(event)
                    df.iloc[i, df.columns.get_loc("bos")] = BULL
                    if self.current_trend == BULL:
                        self.bos_count += 1
                    else:
                        self.bos_count = 1
                    self._store_order_block(df, i, last_high, BULL, op, close)

                self.current_trend = BULL

            # ── Bearish break (close < last swing low) ──
            elif c < last_low.price and not self.low_crossed:
                self.low_crossed = True
                if self.current_trend == BULL:
                    # CHOCH — trend reversal from bull to bear
                    event = StructureEvent(i, "CHOCH", BEAR, last_low.price)
                    self.structure_events.append(event)
                    df.iloc[i, df.columns.get_loc("choch")] = BEAR
                    self.bos_count = 0
                    self._store_order_block(df, i, last_low, BEAR, op, close)
                else:
                    # BOS — continuation
                    event = StructureEvent(i, "BOS", BEAR, last_low.price)
                    self.structure_events.append(event)
                    df.iloc[i, df.columns.get_loc("bos")] = BEAR
                    if self.current_trend == BEAR:
                        self.bos_count += 1
                    else:
                        self.bos_count = 1
                    self._store_order_block(df, i, last_low, BEAR, op, close)

                self.current_trend = BEAR

            df.iloc[i, df.columns.get_loc("smc_trend")] = self.current_trend
            df.iloc[i, df.columns.get_loc("bos_count")] = self.bos_count

        # ── 3. Fair Value Gaps ──────────────────────────
        for i in range(2, n):
            h2 = high[i - 2]
            l2 = low[i - 2]
            h0 = high[i]
            l0 = low[i]

            if l0 > h2:  # bullish FVG
                self.fair_value_gaps.append(
                    FairValueGap(i, top=l0, bottom=h2, bias=BULL)
                )
                df.iloc[i, df.columns.get_loc("fvg")] = BULL
            elif h0 < l2:  # bearish FVG
                self.fair_value_gaps.append(
                    FairValueGap(i, top=l2, bottom=h0, bias=BEAR)
                )
                df.iloc[i, df.columns.get_loc("fvg")] = BEAR

        # ── 4. Mitigate order blocks ────────────────────
        for ob in self.order_blocks:
            if ob.mitigated:
                continue
            for i in range(ob.index + 1, n):
                if ob.bias == BEAR and high[i] > ob.high:
                    ob.mitigated = True
                    break
                if ob.bias == BULL and low[i] < ob.low:
                    ob.mitigated = True
                    break

        # ── 5. Trailing extremes (premium/discount) ────
        if n > 0:
            self.trailing_high = float(np.max(high))
            self.trailing_low = float(np.min(low))

        return df

    def _store_order_block(
        self, df, break_index, pivot, bias, opens, closes
    ):
        """Find and store the order block candle before the structure break."""
        for j in range(break_index - 1, max(0, break_index - 10), -1):
            if bias == BULL and closes[j] < opens[j]:
                # Bullish OB = last bearish candle before bullish break
                ob = OrderBlock(
                    index=j,
                    high=df["high"].iloc[j],
                    low=df["low"].iloc[j],
                    bias=BULL,
                )
                self.order_blocks.append(ob)
                df.iloc[j, df.columns.get_loc("order_block")] = BULL
                break
            elif bias == BEAR and closes[j] > opens[j]:
                # Bearish OB = last bullish candle before bearish break
                ob = OrderBlock(
                    index=j,
                    high=df["high"].iloc[j],
                    low=df["low"].iloc[j],
                    bias=BEAR,
                )
                self.order_blocks.append(ob)
                df.iloc[j, df.columns.get_loc("order_block")] = BEAR
                break

    def get_premium_discount(self, price: float) -> str:
        """Determine if price is in premium, discount, or equilibrium zone."""
        if self.trailing_high <= self.trailing_low:
            return "equilibrium"
        mid = (self.trailing_high + self.trailing_low) / 2.0
        range_size = self.trailing_high - self.trailing_low
        if price > mid + 0.1 * range_size:
            return "premium"
        elif price < mid - 0.1 * range_size:
            return "discount"
        return "equilibrium"

    def get_active_order_blocks(self, bias: int, lookback: int = 20) -> List[OrderBlock]:
        """Get non-mitigated order blocks of given bias in recent bars."""
        active = []
        for ob in reversed(self.order_blocks):
            if ob.mitigated:
                continue
            if ob.bias == bias:
                active.append(ob)
            if len(active) >= 5:
                break
        return active

    def get_recent_fvgs(self, bias: int, lookback: int = 10) -> List[FairValueGap]:
        """Get recent unfilled FVGs of given bias."""
        return [
            fvg for fvg in self.fair_value_gaps[-lookback:]
            if fvg.bias == bias and not fvg.filled
        ]


# ───────────────────────────────────────────────────────────
# Signal Generator — Combined SMC + SuperTrend
# ───────────────────────────────────────────────────────────

def generate_signal(
    df: pd.DataFrame,
    swing_length: int = 5,
    min_bos_count: int = 2,
    supertrend_atr_period: int = 10,
    supertrend_multiplier: float = 3.0,
) -> SignalResult:
    """
    Run SMC + SuperTrend analysis and produce a trading signal.

    Entry logic:
      - Bullish CHOCH on last candle (trend was bearish, broke bullish)
      - Prior trend had ≥ min_bos_count bearish BOS (real trend existed)
      - SuperTrend confirms bullish (dir == 1)
      - Bonus confluence: bullish OB nearby, bullish FVG, discount zone
      → BUY

      Mirror for SELL.

    Exit logic:
      - CHOCH in opposite direction → exit + flip signal

    Args:
        df: OHLCV DataFrame (open, high, low, close columns required).
        swing_length: Bars lookback for swing detection.
        min_bos_count: Min BOS events before CHOCH triggers a signal.
        supertrend_atr_period: ATR period for SuperTrend.
        supertrend_multiplier: ATR multiplier for SuperTrend.

    Returns:
        SignalResult with signal, exit info, and confluence details.
    """
    default = SignalResult(
        signal=0, exit_signal=False, exit_reason="none",
        smc_trend=0, supertrend_dir=0, bos_count=0,
        last_event="insufficient_data", confluence=[],
    )

    if len(df) < 20:
        return default

    # ── 1. Run SuperTrend ─────────────────────────────
    df = compute_supertrend(df, supertrend_atr_period, supertrend_multiplier)

    # ── 2. Run SMC analysis ───────────────────────────
    engine = SMCEngine(swing_length=swing_length, min_bos_count=min_bos_count)
    df = engine.analyze(df)

    last = df.iloc[-1]
    last_close = float(last["close"])
    st_dir = int(last["supertrend_dir"])
    smc_trend = int(last["smc_trend"])
    bos_count = int(last["bos_count"])

    # ── 3. Check for structure events on recent bars ──
    # Look at last 3 bars for CHOCH (it may not be exactly on the last bar)
    lookback = min(3, len(df))
    recent_choch = None
    recent_bos_before_choch = 0

    for i in range(-lookback, 0):
        row = df.iloc[i]
        if row["choch"] != 0:
            recent_choch = int(row["choch"])
            # Count BOS events that preceded this CHOCH
            # Look at the structure events before the CHOCH
            idx = len(df) + i
            prev_rows = df.iloc[max(0, idx - 50):idx]
            if recent_choch == BULL:
                # Count bearish BOS before bullish CHOCH
                recent_bos_before_choch = int((prev_rows["bos"] == BEAR).sum())
            else:
                # Count bullish BOS before bearish CHOCH
                recent_bos_before_choch = int((prev_rows["bos"] == BULL).sum())

    # ── 4. Build confluence factors ───────────────────
    confluence = []

    # Order blocks
    if recent_choch == BULL:
        bull_obs = engine.get_active_order_blocks(BULL)
        if any(ob.low <= last_close <= ob.high for ob in bull_obs):
            confluence.append("price_at_bullish_OB")
        elif bull_obs:
            confluence.append("bullish_OB_nearby")
    elif recent_choch == BEAR:
        bear_obs = engine.get_active_order_blocks(BEAR)
        if any(ob.low <= last_close <= ob.high for ob in bear_obs):
            confluence.append("price_at_bearish_OB")
        elif bear_obs:
            confluence.append("bearish_OB_nearby")

    # Fair value gaps
    if recent_choch == BULL and engine.get_recent_fvgs(BULL):
        confluence.append("bullish_FVG")
    elif recent_choch == BEAR and engine.get_recent_fvgs(BEAR):
        confluence.append("bearish_FVG")

    # Premium / Discount zones
    zone = engine.get_premium_discount(last_close)
    if recent_choch == BULL and zone == "discount":
        confluence.append("discount_zone")
    elif recent_choch == BEAR and zone == "premium":
        confluence.append("premium_zone")

    # ── 5. Generate signal ────────────────────────────
    signal = 0
    exit_signal = False
    exit_reason = "none"
    last_event = "no_event"

    if recent_choch is not None:
        if recent_choch == BULL:
            last_event = f"bullish_CHOCH (after {recent_bos_before_choch} bearish BOS)"
            enough_bos = recent_bos_before_choch >= min_bos_count
            st_agrees = st_dir == 1

            if enough_bos and st_agrees:
                signal = 1  # BUY
                confluence.append("supertrend_bullish")
                logger.info(
                    f"🟢 BUY signal | Bullish CHOCH after {recent_bos_before_choch} "
                    f"bearish BOS | SuperTrend ✓ | Confluence: {confluence}"
                )
            elif enough_bos:
                last_event += " [SuperTrend disagrees]"
                logger.debug(
                    f"Bullish CHOCH detected but SuperTrend bearish — no signal"
                )
            else:
                last_event += f" [need {min_bos_count} BOS, got {recent_bos_before_choch}]"

            # Exit signal: if we were SHORT, this CHOCH means exit
            exit_signal = True
            exit_reason = "choch_reversal"

        elif recent_choch == BEAR:
            last_event = f"bearish_CHOCH (after {recent_bos_before_choch} bullish BOS)"
            enough_bos = recent_bos_before_choch >= min_bos_count
            st_agrees = st_dir == -1

            if enough_bos and st_agrees:
                signal = -1  # SELL
                confluence.append("supertrend_bearish")
                logger.info(
                    f"🔴 SELL signal | Bearish CHOCH after {recent_bos_before_choch} "
                    f"bullish BOS | SuperTrend ✓ | Confluence: {confluence}"
                )
            elif enough_bos:
                last_event += " [SuperTrend disagrees]"
                logger.debug(
                    f"Bearish CHOCH detected but SuperTrend bullish — no signal"
                )
            else:
                last_event += f" [need {min_bos_count} BOS, got {recent_bos_before_choch}]"

            # Exit signal: if we were LONG, this CHOCH means exit
            exit_signal = True
            exit_reason = "choch_reversal"
    else:
        # Check for BOS continuation (just log, no trade signal)
        last_bos = int(last.get("bos", 0))
        if last_bos == BULL:
            last_event = f"bullish_BOS (count: {bos_count})"
        elif last_bos == BEAR:
            last_event = f"bearish_BOS (count: {bos_count})"
        else:
            last_event = "no_structure_event"

    return SignalResult(
        signal=signal,
        exit_signal=exit_signal,
        exit_reason=exit_reason,
        smc_trend=smc_trend,
        supertrend_dir=st_dir,
        bos_count=bos_count,
        last_event=last_event,
        confluence=confluence,
    )