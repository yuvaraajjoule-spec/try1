"""
logic.py — SuperTrend Sniper: Pure SuperTrend Strategy Engine

Direct translation of the Pine Script SuperTrend indicator into Python.
Every trend flip = immediate trade signal. No voting, no thresholds.

Pine Script logic (v4):
    ATR Period = 10, Multiplier = 3.0, Source = hl2
    up   = src - (mult × ATR)       → ratchets UP only in uptrend
    dn   = src + (mult × ATR)       → ratchets DOWN only in downtrend
    trend flips when price crosses the opposite band
    BUY  = trend changes from -1 → +1
    SELL = trend changes from +1 → -1

Fee-aware layer:
    dYdX charges volume-based trading fees (no gas).
    A fee recoup filter ensures the expected move (ATR-based)
    exceeds 2× round-trip fee cost before entering.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────
# Data Classes
# ───────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    """Signal output from SuperTrend Sniper — compatible with bot.py."""
    signal: int             # 1=BUY, -1=SELL, 0=HOLD
    score: float            # trend strength (0–100 scale for display compat)
    exit_signal: bool       # True if current position should be closed
    exit_reason: str        # reason for exit
    regime: str             # "dead", "normal", "volatile"
    trailing_sl: float      # SuperTrend band = natural trailing stop
    indicator_votes: Dict[str, float]  # compat: single entry for SuperTrend
    confluence: List[str]   # list of confirming factors
    atr: float              # current ATR value for position management


# ───────────────────────────────────────────────────────────
# Core: ATR Computation
# ───────────────────────────────────────────────────────────

def compute_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 10,
    use_true_atr: bool = True,
) -> np.ndarray:
    """
    Compute Average True Range.
    If use_true_atr is True, uses Wilder's smoothed ATR (matches Pine atr()).
    If False, uses simple moving average of TR (matches Pine sma(tr, period)).
    """
    n = len(close)
    tr = np.zeros(n)

    # True Range: max(high-low, |high-prev_close|, |low-prev_close|)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)

    atr = np.zeros(n)
    if use_true_atr:
        # Wilder's smoothed ATR (RMA) — matches Pine Script atr()
        atr[0] = tr[0]
        for i in range(1, n):
            if i < period:
                atr[i] = np.mean(tr[:i + 1])
            else:
                atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    else:
        # Simple moving average of TR — matches Pine sma(tr, Periods)
        for i in range(n):
            start = max(0, i - period + 1)
            atr[i] = np.mean(tr[start:i + 1])

    return atr


# ───────────────────────────────────────────────────────────
# Core: SuperTrend Computation (1:1 Pine Script Translation)
# ───────────────────────────────────────────────────────────

def compute_supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_period: int = 10,
    multiplier: float = 3.0,
    use_true_atr: bool = True,
) -> dict:
    """
    Exact translation of Pine Script SuperTrend indicator.

    Returns dict with:
        trend:    array of +1 (bullish) / -1 (bearish)
        up:       upper SuperTrend band (support in uptrend)
        dn:       lower SuperTrend band (resistance in downtrend)
        atr:      ATR array
    """
    n = len(close)
    atr = compute_atr(high, low, close, atr_period, use_true_atr)

    # Source = hl2 = (high + low) / 2
    src = (high + low) / 2.0

    # Raw bands
    up = np.zeros(n)   # support band (price - mult*ATR)
    dn = np.zeros(n)   # resistance band (price + mult*ATR)
    trend = np.ones(n, dtype=int)  # 1 = bullish, -1 = bearish

    up[0] = src[0] - multiplier * atr[0]
    dn[0] = src[0] + multiplier * atr[0]

    for i in range(1, n):
        # Raw band values
        raw_up = src[i] - multiplier * atr[i]
        raw_dn = src[i] + multiplier * atr[i]

        # Pine: up := close[1] > up1 ? max(up, up1) : up
        # Support band ratchets UP — never decreases while in uptrend
        if close[i - 1] > up[i - 1]:
            up[i] = max(raw_up, up[i - 1])
        else:
            up[i] = raw_up

        # Pine: dn := close[1] < dn1 ? min(dn, dn1) : dn
        # Resistance band ratchets DOWN — never increases while in downtrend
        if close[i - 1] < dn[i - 1]:
            dn[i] = min(raw_dn, dn[i - 1])
        else:
            dn[i] = raw_dn

        # Pine: trend := trend == -1 and close > dn1 ? 1 :
        #                trend == 1 and close < up1 ? -1 : trend
        prev_trend = trend[i - 1]
        if prev_trend == -1 and close[i] > dn[i - 1]:
            trend[i] = 1
        elif prev_trend == 1 and close[i] < up[i - 1]:
            trend[i] = -1
        else:
            trend[i] = prev_trend

    return {
        "trend": trend,
        "up": up,
        "dn": dn,
        "atr": atr,
    }


# ───────────────────────────────────────────────────────────
# Volatility Regime (lightweight — ATR percentile only)
# ───────────────────────────────────────────────────────────

def classify_regime(atr: np.ndarray, lookback: int = 100) -> str:
    """
    Quick regime classification from ATR percentile.
    dead    = ATR < 20th percentile (skip — whipsaw zone)
    normal  = 20th–75th
    volatile = > 75th (good for momentum trades)
    """
    window = atr[-min(lookback, len(atr)):]
    if len(window) < 10:
        return "normal"

    current = atr[-1]
    pctile = np.sum(window < current) / len(window)

    if pctile < 0.20:
        return "dead"
    elif pctile > 0.75:
        return "volatile"
    return "normal"


# ───────────────────────────────────────────────────────────
# Fee Recoup Filter
# ───────────────────────────────────────────────────────────

def passes_fee_filter(
    atr_value: float,
    price: float,
    fee_pct: float = 0.05,
    min_atr_to_fee_ratio: float = 2.0,
) -> bool:
    """
    Ensure the expected move (ATR) is at least min_atr_to_fee_ratio × round-trip fees.

    dYdX fees: ~0.02% maker / ~0.05% taker per side.
    Round trip cost = 2 × fee_pct (open + close).

    If ATR / price < min_ratio × round_trip_fee → skip (likely unprofitable).
    """
    if price <= 0 or atr_value <= 0:
        return False

    round_trip_fee = 2.0 * (fee_pct / 100.0)
    atr_as_pct = atr_value / price
    return atr_as_pct >= min_atr_to_fee_ratio * round_trip_fee


# ───────────────────────────────────────────────────────────
# SuperTrend Sniper Engine
# ───────────────────────────────────────────────────────────

class SuperTrendEngine:
    """
    Lean, fast SuperTrend signal generator.
    Every trend flip = trade. No thresholds, no voting.
    """

    def __init__(
        self,
        atr_period: int = 10,
        multiplier: float = 3.0,
        use_true_atr: bool = True,
        trailing_atr_mult: float = 1.5,
        max_hold_candles: int = 60,
        fee_filter_enabled: bool = True,
        estimated_fee_pct: float = 0.05,
    ):
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.use_true_atr = use_true_atr
        self.trailing_atr_mult = trailing_atr_mult
        self.max_hold_candles = max_hold_candles
        self.fee_filter_enabled = fee_filter_enabled
        self.estimated_fee_pct = estimated_fee_pct

    def analyze(self, df: pd.DataFrame) -> SignalResult:
        """Run SuperTrend on OHLCV DataFrame and produce a signal."""
        default = SignalResult(
            signal=0, score=0.0, exit_signal=False, exit_reason="none",
            regime="unknown", trailing_sl=0.0, indicator_votes={},
            confluence=[], atr=0.0,
        )

        min_bars = self.atr_period + 5
        if len(df) < min_bars:
            default.exit_reason = "insufficient_data"
            return default

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        # ── Compute SuperTrend ────────────────────────
        st = compute_supertrend(
            high, low, close,
            atr_period=self.atr_period,
            multiplier=self.multiplier,
            use_true_atr=self.use_true_atr,
        )

        trend = st["trend"]
        up_band = st["up"]
        dn_band = st["dn"]
        atr_arr = st["atr"]

        current_trend = trend[-1]
        prev_trend = trend[-2] if len(trend) > 1 else current_trend
        current_atr = float(atr_arr[-1])
        current_price = float(close[-1])

        # ── Regime classification ─────────────────────
        regime = classify_regime(atr_arr)

        # ── Signal detection (Pine Script: buySignal / sellSignal) ──
        #   buySignal  = trend == 1 and trend[1] == -1
        #   sellSignal = trend == -1 and trend[1] == 1
        buy_flip = current_trend == 1 and prev_trend == -1
        sell_flip = current_trend == -1 and prev_trend == 1

        signal = 0
        confluence = []

        if buy_flip:
            signal = 1
            confluence.append("supertrend_flip_bull")
        elif sell_flip:
            signal = -1
            confluence.append("supertrend_flip_bear")

        # ── Regime filter: skip signals in dead markets ──
        if signal != 0 and regime == "dead":
            logger.info(
                f"💀 SuperTrend flip filtered — dead market "
                f"(ATR={current_atr:.2f})"
            )
            signal = 0
            confluence.append("filtered_dead_market")

        # ── Fee recoup filter ─────────────────────────
        if signal != 0 and self.fee_filter_enabled:
            if not passes_fee_filter(current_atr, current_price, self.estimated_fee_pct):
                logger.info(
                    f"💸 SuperTrend flip filtered — ATR too low for fees "
                    f"(ATR={current_atr:.2f}, price=${current_price:,.2f})"
                )
                signal = 0
                confluence.append("filtered_fee_recoup")

        # ── Natural trailing stop from SuperTrend bands ──
        trailing_sl = 0.0
        if current_trend == 1:
            # In uptrend: the UP band IS the trailing stop
            trailing_sl = float(up_band[-1])
            confluence.append(f"st_support_{trailing_sl:.2f}")
        elif current_trend == -1:
            # In downtrend: the DN band IS the trailing stop
            trailing_sl = float(dn_band[-1])
            confluence.append(f"st_resistance_{trailing_sl:.2f}")

        # ── Exit signal: trend still holds but check for waning momentum ──
        exit_signal = False
        exit_reason = "none"

        # Score for display (0–100 scale):
        #   Based on how far price is from the SuperTrend band (trend strength)
        if current_trend == 1:
            dist = (current_price - up_band[-1]) / current_atr if current_atr > 0 else 0
            score = min(max(dist * 25, 10), 100)  # 10–100 scale
        else:
            dist = (dn_band[-1] - current_price) / current_atr if current_atr > 0 else 0
            score = -min(max(dist * 25, 10), 100)  # -10 to -100

        # Votes dict for compat
        indicator_votes = {
            "supertrend": float(np.sign(current_trend)),
        }

        if signal != 0:
            label = "BUY 🟢" if signal == 1 else "SELL 🔴"
            logger.info(
                f"⚡ {label} | SuperTrend Flip! | "
                f"Price: ${current_price:,.2f} | ATR: {current_atr:.2f} | "
                f"Regime: {regime} | "
                f"Band: ${trailing_sl:,.2f}"
            )

        return SignalResult(
            signal=signal,
            score=score,
            exit_signal=exit_signal,
            exit_reason=exit_reason,
            regime=regime,
            trailing_sl=round(trailing_sl, 2),
            indicator_votes=indicator_votes,
            confluence=confluence,
            atr=current_atr,
        )


# ───────────────────────────────────────────────────────────
# Public API — drop-in replacement for old generate_signal()
# ───────────────────────────────────────────────────────────

def generate_signal(
    df: pd.DataFrame,
    st_atr_period: int = 10,
    st_multiplier: float = 3.0,
    st_use_true_atr: bool = True,
    trailing_atr_mult: float = 1.5,
    max_hold_candles: int = 60,
    fee_filter_enabled: bool = True,
    estimated_fee_pct: float = 0.05,
    **kwargs,  # absorb any old Hydra params gracefully
) -> SignalResult:
    """
    Run SuperTrend Sniper analysis and produce a trading signal.
    Drop-in replacement for the old Hydra Engine generate_signal().
    """
    engine = SuperTrendEngine(
        atr_period=st_atr_period,
        multiplier=st_multiplier,
        use_true_atr=st_use_true_atr,
        trailing_atr_mult=trailing_atr_mult,
        max_hold_candles=max_hold_candles,
        fee_filter_enabled=fee_filter_enabled,
        estimated_fee_pct=estimated_fee_pct,
    )
    return engine.analyze(df)