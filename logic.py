"""
logic.py — Pure SuperTrend Signal Engine

Exact 1:1 translation of the Pine Script v4 SuperTrend indicator.
No extra filters. No voting. No regime. No trailing stops.

    buySignal  = trend == 1 and trend[1] == -1   →  BUY
    sellSignal = trend == -1 and trend[1] == 1    →  SELL

The bot layer adds:
    • 3-second confirmation (re-fetch candles after 3s, signal must persist)
    • Minimum ATR filter (skip when move is too small to be worth trading)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────
# Signal Output
# ───────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    """What the bot needs to act on."""
    signal: int        # 1=BUY, -1=SELL, 0=HOLD
    atr: float         # current ATR value
    price: float       # latest close price
    st_band: float     # active SuperTrend band (support/resistance)


# ───────────────────────────────────────────────────────────
# ATR — Wilder's smoothed (Pine Script `atr()`)
# ───────────────────────────────────────────────────────────

def _compute_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int,
    use_wilder: bool = True,
) -> np.ndarray:
    """
    Pine Script:
        atr2 = sma(tr, Periods)
        atr  = changeATR ? atr(Periods) : atr2

    changeATR defaults to true → Wilder's smoothed ATR.
    """
    n = len(close)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    atr = np.zeros(n)
    if use_wilder:
        # Wilder's RMA — matches Pine atr()
        atr[0] = tr[0]
        for i in range(1, n):
            if i < period:
                atr[i] = np.mean(tr[: i + 1])
            else:
                atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    else:
        # SMA of TR — matches Pine sma(tr, Periods)
        for i in range(n):
            start = max(0, i - period + 1)
            atr[i] = np.mean(tr[start : i + 1])

    return atr


# ───────────────────────────────────────────────────────────
# SuperTrend — Exact Pine Script v4 Translation
# ───────────────────────────────────────────────────────────

def compute_supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 10,
    multiplier: float = 3.0,
    use_wilder: bool = True,
) -> dict:
    """
    Line-by-line translation of:

        src = hl2
        up  = src - (Multiplier * atr)
        up1 = nz(up[1], up)
        up := close[1] > up1 ? max(up, up1) : up

        dn  = src + (Multiplier * atr)
        dn1 = nz(dn[1], dn)
        dn := close[1] < dn1 ? min(dn, dn1) : dn

        trend = 1
        trend := nz(trend[1], trend)
        trend := trend == -1 and close > dn1 ? 1 :
                 trend == 1  and close < up1 ? -1 : trend

        buySignal  = trend == 1  and trend[1] == -1
        sellSignal = trend == -1 and trend[1] == 1
    """
    n = len(close)
    atr = _compute_atr(high, low, close, period, use_wilder)

    # src = hl2
    src = (high + low) / 2.0

    up = np.zeros(n)
    dn = np.zeros(n)
    trend = np.ones(n, dtype=int)

    # Bar 0 — initial values
    up[0] = src[0] - multiplier * atr[0]
    dn[0] = src[0] + multiplier * atr[0]

    for i in range(1, n):
        # Raw bands
        raw_up = src[i] - multiplier * atr[i]
        raw_dn = src[i] + multiplier * atr[i]

        # up := close[1] > up1 ? max(up, up1) : up
        if close[i - 1] > up[i - 1]:
            up[i] = max(raw_up, up[i - 1])
        else:
            up[i] = raw_up

        # dn := close[1] < dn1 ? min(dn, dn1) : dn
        if close[i - 1] < dn[i - 1]:
            dn[i] = min(raw_dn, dn[i - 1])
        else:
            dn[i] = raw_dn

        # trend := trend == -1 and close > dn1 ? 1 :
        #          trend == 1  and close < up1 ? -1 : trend
        prev = trend[i - 1]
        if prev == -1 and close[i] > dn[i - 1]:
            trend[i] = 1
        elif prev == 1 and close[i] < up[i - 1]:
            trend[i] = -1
        else:
            trend[i] = prev

    return {"trend": trend, "up": up, "dn": dn, "atr": atr}


# ───────────────────────────────────────────────────────────
# Public API — generate_signal()
# ───────────────────────────────────────────────────────────

def generate_signal(
    df: pd.DataFrame,
    atr_period: int = 10,
    multiplier: float = 3.0,
    use_wilder: bool = True,
    min_atr_pct: float = 0.0005,
    **kwargs,
) -> SignalResult:
    """
    Run SuperTrend on OHLCV DataFrame → return signal.

    min_atr_pct: minimum ATR as fraction of price to take a trade.
                 e.g. 0.0005 = 0.05%. Anything smaller is "too small".
    """
    hold = SignalResult(signal=0, atr=0.0, price=0.0, st_band=0.0)

    if len(df) < atr_period + 2:
        return hold

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    st = compute_supertrend(high, low, close, atr_period, multiplier, use_wilder)

    trend = st["trend"]
    up = st["up"]
    dn = st["dn"]
    atr_arr = st["atr"]

    current_price = float(close[-1])
    current_atr = float(atr_arr[-1])
    current_trend = int(trend[-1])
    prev_trend = int(trend[-2])

    # Active band
    st_band = float(up[-1]) if current_trend == 1 else float(dn[-1])

    # ── Signal: exact Pine Script logic ──
    # buySignal  = trend == 1  and trend[1] == -1
    # sellSignal = trend == -1 and trend[1] == 1
    signal = 0
    if current_trend == 1 and prev_trend == -1:
        signal = 1   # BUY
    elif current_trend == -1 and prev_trend == 1:
        signal = -1  # SELL

    # ── Skip small moves ──
    if signal != 0 and current_price > 0:
        atr_pct = current_atr / current_price
        if atr_pct < min_atr_pct:
            logger.info(
                f"⏭ Signal skipped — move too small | "
                f"ATR%: {atr_pct*100:.4f}% < min {min_atr_pct*100:.4f}%"
            )
            signal = 0

    if signal != 0:
        label = "BUY 🟢" if signal == 1 else "SELL 🔴"
        logger.info(
            f"⚡ {label} | Price: ${current_price:,.2f} | "
            f"ATR: {current_atr:.2f} | Band: ${st_band:,.2f}"
        )

    return SignalResult(
        signal=signal,
        atr=current_atr,
        price=current_price,
        st_band=st_band,
    )