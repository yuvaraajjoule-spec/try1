"""
logic.py — Hydra Engine: Multi-Indicator Adaptive Scalping Strategy

7 independent signal generators vote with configurable weights.
A trade triggers when the weighted score crosses a threshold.

Signal Heads:
  1. EMA Ribbon (8/13/21/55)      — Trend direction & alignment   (20%)
  2. RSI (7) + Divergence          — Momentum & exhaustion         (15%)
  3. VWAP Bands (±1σ, ±2σ)        — Institutional fair value      (15%)
  4. Bollinger Band Squeeze        — Volatility breakout           (15%)
  5. Keltner Channel               — Breakout confirmation         (10%)
  6. Volume Profile (RVOL)         — Volume confirmation           (15%)
  7. ATR Regime Filter             — Volatility regime gate        (10%)

Exit layers:
  - Trailing stop (ATR-based, tightens with profit)
  - Partial TP at 1× ATR
  - Score reversal exit
  - Time-based exit (max hold candles)
  - Emergency SL (hard %)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BULL = 1
BEAR = -1


# ───────────────────────────────────────────────────────────
# Data Classes
# ───────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    """Rich signal output from the Hydra Engine."""
    signal: int             # 1=BUY, -1=SELL, 0=HOLD
    score: float            # aggregate weighted score (-100 to +100)
    exit_signal: bool       # True if current position should be closed
    exit_reason: str        # reason for exit
    regime: str             # "dead", "normal", "volatile"
    trailing_sl: float      # computed trailing stop level
    indicator_votes: Dict[str, float]  # per-indicator vote detail
    confluence: List[str]   # list of confirming factors
    atr: float              # current ATR value for position mgmt


# ───────────────────────────────────────────────────────────
# Indicator 1: EMA Ribbon (weight: 20%)
# ───────────────────────────────────────────────────────────

def compute_ema_ribbon(
    df: pd.DataFrame,
    fast: int = 8,
    mid1: int = 13,
    mid2: int = 21,
    slow: int = 55,
) -> Dict:
    """
    Compute 4-EMA ribbon alignment.
    Returns vote: +1 (bullish aligned), -1 (bearish aligned), 0 (mixed).
    Also returns trend strength (how many EMA pairs are in order).
    """
    close = df["close"]
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_m1 = close.ewm(span=mid1, adjust=False).mean()
    ema_m2 = close.ewm(span=mid2, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()

    f = ema_f.iloc[-1]
    m1 = ema_m1.iloc[-1]
    m2 = ema_m2.iloc[-1]
    s = ema_s.iloc[-1]
    price = close.iloc[-1]

    # Count aligned pairs for strength
    bull_pairs = sum([
        price > f,
        f > m1,
        m1 > m2,
        m2 > s,
    ])
    bear_pairs = sum([
        price < f,
        f < m1,
        m1 < m2,
        m2 < s,
    ])

    # Check for EMA crossover on last 2 bars (momentum trigger)
    cross_bull = ema_f.iloc[-1] > ema_m1.iloc[-1] and ema_f.iloc[-2] <= ema_m1.iloc[-2]
    cross_bear = ema_f.iloc[-1] < ema_m1.iloc[-1] and ema_f.iloc[-2] >= ema_m1.iloc[-2]

    if bull_pairs >= 3:
        vote = min(bull_pairs / 4.0, 1.0)
        if cross_bull:
            vote = min(vote + 0.25, 1.0)
    elif bear_pairs >= 3:
        vote = -min(bear_pairs / 4.0, 1.0)
        if cross_bear:
            vote = max(vote - 0.25, -1.0)
    else:
        vote = 0.0

    factors = []
    if bull_pairs == 4:
        factors.append("ema_perfect_bull")
    elif bear_pairs == 4:
        factors.append("ema_perfect_bear")
    if cross_bull:
        factors.append("ema_cross_bull")
    if cross_bear:
        factors.append("ema_cross_bear")

    return {"vote": vote, "factors": factors}


# ───────────────────────────────────────────────────────────
# Indicator 2: RSI + Divergence (weight: 15%)
# ───────────────────────────────────────────────────────────

def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Compute RSI array."""
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.zeros_like(close)
    avg_loss = np.zeros_like(close)

    if len(close) > period:
        avg_gain[period] = np.mean(gain[1:period + 1])
        avg_loss[period] = np.mean(loss[1:period + 1])
        for i in range(period + 1, len(close)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def compute_rsi_signal(df: pd.DataFrame, period: int = 7) -> Dict:
    """
    RSI with divergence detection.
    Vote: +1 (oversold bounce / bullish div), -1 (overbought reject / bearish div), 0 neutral.
    """
    close = df["close"].values
    rsi = _rsi(close, period)

    current_rsi = rsi[-1]
    prev_rsi = rsi[-2] if len(rsi) > 1 else 50.0

    vote = 0.0
    factors = []

    # Oversold/overbought zones
    if current_rsi < 30:
        if current_rsi > prev_rsi:  # bouncing up from oversold
            vote = 0.8
            factors.append("rsi_oversold_bounce")
        else:
            vote = 0.4  # still oversold but falling — partial bull
            factors.append("rsi_oversold")
    elif current_rsi > 70:
        if current_rsi < prev_rsi:  # dropping from overbought
            vote = -0.8
            factors.append("rsi_overbought_reject")
        else:
            vote = -0.4
            factors.append("rsi_overbought")
    elif 45 <= current_rsi <= 55:
        vote = 0.0  # neutral zone
    elif current_rsi > 55:
        vote = (current_rsi - 50) / 50.0  # mild bullish 0-0.4
    else:
        vote = -(50 - current_rsi) / 50.0  # mild bearish 0 to -0.4

    # Divergence detection (look back 10 bars)
    lookback = min(10, len(close) - 1)
    if lookback >= 5:
        price_window = close[-lookback:]
        rsi_window = rsi[-lookback:]

        # Bullish divergence: price making lower low, RSI making higher low
        price_ll = price_window[-1] < np.min(price_window[:-1])
        rsi_hl = rsi_window[-1] > np.min(rsi_window[:-1])
        if price_ll and rsi_hl and current_rsi < 40:
            vote = max(vote, 0.9)
            factors.append("rsi_bullish_divergence")

        # Bearish divergence: price making higher high, RSI making lower high
        price_hh = price_window[-1] > np.max(price_window[:-1])
        rsi_lh = rsi_window[-1] < np.max(rsi_window[:-1])
        if price_hh and rsi_lh and current_rsi > 60:
            vote = min(vote, -0.9)
            factors.append("rsi_bearish_divergence")

    return {"vote": np.clip(vote, -1.0, 1.0), "factors": factors}


# ───────────────────────────────────────────────────────────
# Indicator 3: VWAP Bands (weight: 15%)
# ───────────────────────────────────────────────────────────

def compute_vwap_bands(df: pd.DataFrame) -> Dict:
    """
    Compute VWAP with ±1σ and ±2σ bands.
    Vote based on price position relative to VWAP.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].replace(0, np.nan).fillna(1.0)

    cum_vol = volume.cumsum()
    cum_tp_vol = (typical * volume).cumsum()
    vwap = cum_tp_vol / cum_vol

    # Standard deviation bands
    cum_tp2_vol = (typical ** 2 * volume).cumsum()
    variance = (cum_tp2_vol / cum_vol) - (vwap ** 2)
    variance = variance.clip(lower=0)
    std = np.sqrt(variance)

    price = df["close"].iloc[-1]
    v = vwap.iloc[-1]
    s = max(std.iloc[-1], 0.0001)

    band_1_upper = v + s
    band_1_lower = v - s
    band_2_upper = v + 2 * s
    band_2_lower = v - 2 * s

    vote = 0.0
    factors = []

    deviation = (price - v) / s if s > 0 else 0

    if price <= band_2_lower:
        vote = 0.9  # extreme discount — mean reversion long
        factors.append("vwap_extreme_discount")
    elif price <= band_1_lower:
        vote = 0.6
        factors.append("vwap_discount")
    elif price >= band_2_upper:
        vote = -0.9  # extreme premium — mean reversion short
        factors.append("vwap_extreme_premium")
    elif price >= band_1_upper:
        vote = -0.6
        factors.append("vwap_premium")
    else:
        # Near VWAP — check direction
        prev_price = df["close"].iloc[-2]
        if price > v and prev_price <= v:
            vote = 0.3
            factors.append("vwap_cross_above")
        elif price < v and prev_price >= v:
            vote = -0.3
            factors.append("vwap_cross_below")

    return {"vote": np.clip(vote, -1.0, 1.0), "factors": factors, "vwap": v}


# ───────────────────────────────────────────────────────────
# Indicator 4: Bollinger Band Squeeze (weight: 15%)
# ───────────────────────────────────────────────────────────

def compute_bollinger(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> Dict:
    """
    Bollinger Bands with squeeze detection.
    Squeeze = bands contracting (low volatility) → expansion = breakout signal.
    """
    close = df["close"]
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()

    upper = sma + num_std * std
    lower = sma - num_std * std
    bandwidth = ((upper - lower) / sma * 100)

    price = close.iloc[-1]
    u = upper.iloc[-1]
    l = lower.iloc[-1]
    m = sma.iloc[-1]

    vote = 0.0
    factors = []

    # Detect squeeze: bandwidth in bottom 20th percentile of last 50 bars
    bw_window = bandwidth.dropna().tail(50)
    if len(bw_window) >= 10:
        bw_pctile = (bandwidth.iloc[-1] - bw_window.min()) / (bw_window.max() - bw_window.min() + 0.0001)
        is_squeeze = bw_pctile < 0.20
        expanding = bandwidth.iloc[-1] > bandwidth.iloc[-2] if len(bandwidth) > 1 else False

        if is_squeeze and expanding:
            # Squeeze firing — direction from price vs middle band
            if price > m:
                vote = 0.8
                factors.append("bb_squeeze_bull_breakout")
            else:
                vote = -0.8
                factors.append("bb_squeeze_bear_breakout")
        elif is_squeeze:
            factors.append("bb_in_squeeze")
            # No directional vote during squeeze (waiting)
        else:
            # Normal BB — check for band walks and bounces
            bb_pct = (price - l) / (u - l + 0.0001)

            if bb_pct >= 0.95:  # walking upper band
                if close.iloc[-2] < upper.iloc[-2]:
                    vote = 0.5  # just broke above — bullish
                    factors.append("bb_upper_break")
                else:
                    vote = -0.3  # extended — possible reversal
                    factors.append("bb_upper_extended")
            elif bb_pct <= 0.05:  # walking lower band
                if close.iloc[-2] > lower.iloc[-2]:
                    vote = -0.5
                    factors.append("bb_lower_break")
                else:
                    vote = 0.3  # extreme oversold bounce
                    factors.append("bb_lower_bounce")
            elif 0.4 <= bb_pct <= 0.6:
                vote = 0.0  # middle zone, neutral

    return {"vote": np.clip(vote, -1.0, 1.0), "factors": factors}


# ───────────────────────────────────────────────────────────
# Indicator 5: Keltner Channel (weight: 10%)
# ───────────────────────────────────────────────────────────

def compute_keltner(df: pd.DataFrame, period: int = 20, atr_mult: float = 1.5) -> Dict:
    """
    Keltner Channel breakout detection.
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema_mid = close.ewm(span=period, adjust=False).mean()

    # ATR
    tr = pd.DataFrame({
        "hl": high - low,
        "hc": (high - close.shift(1)).abs(),
        "lc": (low - close.shift(1)).abs(),
    }).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    kc_upper = ema_mid + atr_mult * atr
    kc_lower = ema_mid - atr_mult * atr

    price = close.iloc[-1]
    prev = close.iloc[-2]
    u = kc_upper.iloc[-1]
    l = kc_lower.iloc[-1]
    prev_u = kc_upper.iloc[-2]
    prev_l = kc_lower.iloc[-2]

    vote = 0.0
    factors = []

    if price > u and prev <= prev_u:
        vote = 0.9
        factors.append("keltner_upper_breakout")
    elif price < l and prev >= prev_l:
        vote = -0.9
        factors.append("keltner_lower_breakout")
    elif price > u:
        vote = 0.4  # already above — trend continuation
        factors.append("keltner_above_upper")
    elif price < l:
        vote = -0.4
        factors.append("keltner_below_lower")
    elif price > ema_mid.iloc[-1]:
        vote = 0.1
    else:
        vote = -0.1

    return {"vote": np.clip(vote, -1.0, 1.0), "factors": factors}


# ───────────────────────────────────────────────────────────
# Indicator 6: Volume Profile — RVOL (weight: 15%)
# ───────────────────────────────────────────────────────────

def compute_volume_profile(df: pd.DataFrame, lookback: int = 20) -> Dict:
    """
    Relative Volume (RVOL) — current volume vs average.
    High volume confirms real moves; low volume = fakeout risk.
    """
    vol = df["volume"]
    avg_vol = vol.rolling(lookback).mean()

    current_vol = vol.iloc[-1]
    avg = avg_vol.iloc[-1] if not np.isnan(avg_vol.iloc[-1]) else vol.mean()

    rvol = current_vol / max(avg, 0.001)

    vote = 0.0
    factors = []

    # Volume doesn't have a direction — it amplifies the price direction
    price_change = df["close"].iloc[-1] - df["close"].iloc[-2]

    if rvol >= 2.0:
        # Very high volume — strong confirmation
        vote = 1.0 if price_change > 0 else -1.0
        factors.append(f"rvol_spike_{rvol:.1f}x")
    elif rvol >= 1.5:
        vote = 0.7 if price_change > 0 else -0.7
        factors.append(f"rvol_high_{rvol:.1f}x")
    elif rvol >= 1.0:
        vote = 0.3 if price_change > 0 else -0.3
        factors.append(f"rvol_normal_{rvol:.1f}x")
    else:
        # Low volume — weak signal, penalize
        vote = 0.0
        factors.append(f"rvol_low_{rvol:.1f}x")

    return {"vote": np.clip(vote, -1.0, 1.0), "factors": factors, "rvol": rvol}


# ───────────────────────────────────────────────────────────
# Indicator 7: ATR Regime Filter (weight: 10%)
# ───────────────────────────────────────────────────────────

def compute_atr_regime(df: pd.DataFrame, period: int = 14) -> Dict:
    """
    Classify market volatility into regimes:
      - dead: ATR < 30th percentile → skip trades
      - normal: 30th–70th percentile → standard trading
      - volatile: > 70th percentile → tighter stops, wider targets
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.DataFrame({
        "hl": high - low,
        "hc": (high - close.shift(1)).abs(),
        "lc": (low - close.shift(1)).abs(),
    }).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    current_atr = atr.iloc[-1]

    # Use last 100 bars for percentile ranking
    atr_window = atr.tail(min(100, len(atr)))
    if len(atr_window) < 10:
        return {"vote": 0.0, "factors": ["insufficient_data"], "regime": "normal", "atr": current_atr}

    pctile = (atr_window < current_atr).sum() / len(atr_window)

    if pctile < 0.25:
        regime = "dead"
        vote = 0.0  # don't amplify signals in dead markets
        factors = ["regime_dead"]
    elif pctile > 0.75:
        regime = "volatile"
        vote = 0.5  # volatile = good for scalping (directional based on trend)
        price_dir = df["close"].iloc[-1] - df["close"].iloc[-3]
        if price_dir > 0:
            vote = 0.5
        elif price_dir < 0:
            vote = -0.5
        factors = ["regime_volatile"]
    else:
        regime = "normal"
        vote = 0.3
        price_dir = df["close"].iloc[-1] - df["close"].iloc[-2]
        if price_dir > 0:
            vote = 0.3
        elif price_dir < 0:
            vote = -0.3
        factors = ["regime_normal"]

    return {"vote": np.clip(vote, -1.0, 1.0), "factors": factors, "regime": regime, "atr": float(current_atr)}


# ───────────────────────────────────────────────────────────
# Hydra Engine — Orchestrator
# ───────────────────────────────────────────────────────────

# Default indicator weights (sum to 100)
DEFAULT_WEIGHTS = {
    "ema_ribbon": 20,
    "rsi": 15,
    "vwap": 15,
    "bollinger": 15,
    "keltner": 10,
    "volume": 15,
    "atr_regime": 10,
}


class HydraEngine:
    """
    Multi-indicator voting engine.
    Each indicator returns a vote in [-1, +1].
    Votes are multiplied by weights and summed.
    Trade triggers when |score| >= threshold.
    """

    def __init__(
        self,
        weights: Optional[Dict[str, int]] = None,
        signal_threshold: int = 60,
        ema_fast: int = 8,
        rsi_period: int = 7,
        bb_period: int = 20,
        trailing_atr_mult: float = 1.5,
        max_hold_candles: int = 15,
    ):
        self.weights = weights or dict(DEFAULT_WEIGHTS)
        self.signal_threshold = signal_threshold
        self.ema_fast = ema_fast
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.trailing_atr_mult = trailing_atr_mult
        self.max_hold_candles = max_hold_candles

    def analyze(self, df: pd.DataFrame) -> SignalResult:
        """Run all 7 indicators and produce a weighted signal."""
        default = SignalResult(
            signal=0, score=0.0, exit_signal=False, exit_reason="none",
            regime="unknown", trailing_sl=0.0, indicator_votes={},
            confluence=[], atr=0.0,
        )

        if len(df) < 60:
            default.exit_reason = "insufficient_data"
            return default

        # ── Run each indicator head ───────────────────
        ema_result = compute_ema_ribbon(df, fast=self.ema_fast)
        rsi_result = compute_rsi_signal(df, period=self.rsi_period)
        vwap_result = compute_vwap_bands(df)
        bb_result = compute_bollinger(df, period=self.bb_period)
        kc_result = compute_keltner(df)
        vol_result = compute_volume_profile(df)
        atr_result = compute_atr_regime(df)

        regime = atr_result.get("regime", "normal")
        current_atr = atr_result.get("atr", 0.0)

        # ── Weighted scoring ──────────────────────────
        votes = {
            "ema_ribbon": ema_result["vote"],
            "rsi": rsi_result["vote"],
            "vwap": vwap_result["vote"],
            "bollinger": bb_result["vote"],
            "keltner": kc_result["vote"],
            "volume": vol_result["vote"],
            "atr_regime": atr_result["vote"],
        }

        weighted_score = sum(
            votes[k] * self.weights.get(k, 0) for k in votes
        )
        # Score is already on a -100 to +100 scale

        # ── Regime adjustment ─────────────────────────
        if regime == "dead":
            # In dead markets, require much higher consensus
            effective_threshold = self.signal_threshold * 1.5
            logger.debug(f"Dead market — threshold raised to {effective_threshold}")
        elif regime == "volatile":
            # Volatile = slightly lower threshold (momentum is real)
            effective_threshold = self.signal_threshold * 0.85
        else:
            effective_threshold = float(self.signal_threshold)

        # ── Generate signal ───────────────────────────
        signal = 0
        if weighted_score >= effective_threshold:
            signal = 1  # BUY
        elif weighted_score <= -effective_threshold:
            signal = -1  # SELL

        # ── Collect all confluence factors ────────────
        confluence = []
        for r in [ema_result, rsi_result, vwap_result, bb_result, kc_result, vol_result, atr_result]:
            confluence.extend(r.get("factors", []))

        # ── Compute trailing stop level ───────────────
        price = df["close"].iloc[-1]
        trailing_sl = 0.0
        if signal == 1:
            trailing_sl = price - self.trailing_atr_mult * current_atr
        elif signal == -1:
            trailing_sl = price + self.trailing_atr_mult * current_atr

        # ── Exit signal detection ─────────────────────
        exit_signal = False
        exit_reason = "none"

        # Score reversal: if score strongly opposes current direction
        if weighted_score <= -30:
            exit_signal = True
            exit_reason = "score_reversal_bearish"
        if weighted_score >= 30:
            exit_signal = True
            exit_reason = "score_reversal_bullish"

        if signal != 0:
            label = "BUY 🟢" if signal == 1 else "SELL 🔴"
            logger.info(
                f"🐉 {label} | Score: {weighted_score:+.1f}/{effective_threshold:.0f} | "
                f"Regime: {regime} | Votes: "
                + " | ".join(f"{k}:{v:+.2f}" for k, v in votes.items())
            )

        return SignalResult(
            signal=signal,
            score=weighted_score,
            exit_signal=exit_signal,
            exit_reason=exit_reason,
            regime=regime,
            trailing_sl=round(trailing_sl, 2),
            indicator_votes=votes,
            confluence=confluence,
            atr=current_atr,
        )


# ───────────────────────────────────────────────────────────
# Public API — drop-in replacement for old generate_signal()
# ───────────────────────────────────────────────────────────

def generate_signal(
    df: pd.DataFrame,
    signal_threshold: int = 60,
    ema_fast: int = 8,
    rsi_period: int = 7,
    bb_period: int = 20,
    trailing_atr_mult: float = 1.5,
    max_hold_candles: int = 15,
    **kwargs,  # absorb any old params gracefully
) -> SignalResult:
    """
    Run Hydra Engine analysis and produce a trading signal.
    Drop-in replacement for the old SMC + SuperTrend generate_signal().
    """
    engine = HydraEngine(
        signal_threshold=signal_threshold,
        ema_fast=ema_fast,
        rsi_period=rsi_period,
        bb_period=bb_period,
        trailing_atr_mult=trailing_atr_mult,
        max_hold_candles=max_hold_candles,
    )
    return engine.analyze(df)