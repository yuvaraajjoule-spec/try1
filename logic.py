import pandas as pd
import numpy as np

BULL = 1
BEAR = -1


class SMCIndicator:
    def __init__(self, df: pd.DataFrame):
        """
        df must contain: open, high, low, close
        """
        self.df = df.copy()
        self.df["trend"] = 0
        self.df["bos"] = 0
        self.df["choch"] = 0
        self.df["swing_high"] = np.nan
        self.df["swing_low"] = np.nan
        self.df["fvg"] = 0

        self.swing_high = None
        self.swing_low = None
        self.trend = 0

    # ----------------------------
    # SWING DETECTION
    # ----------------------------
    def detect_swings(self, length=5):
        highs = self.df["high"].values
        lows = self.df["low"].values

        swing_highs = []
        swing_lows = []

        for i in range(length, len(self.df) - length):
            if highs[i] == max(highs[i - length:i + length]):
                swing_highs.append((i, highs[i]))
            if lows[i] == min(lows[i - length:i + length]):
                swing_lows.append((i, lows[i]))

        for i, v in swing_highs:
            self.df.at[self.df.index[i], "swing_high"] = v

        for i, v in swing_lows:
            self.df.at[self.df.index[i], "swing_low"] = v

    # ----------------------------
    # TREND STRUCTURE (BOS / CHOCH)
    # ----------------------------
    def structure(self):
        for i in range(1, len(self.df)):
            close = self.df["close"].iloc[i]

            prev_high = self.df["swing_high"].iloc[:i].dropna()
            prev_low = self.df["swing_low"].iloc[:i].dropna()

            if len(prev_high) == 0 or len(prev_low) == 0:
                continue

            last_high = prev_high.iloc[-1]
            last_low = prev_low.iloc[-1]

            # Break of Structure
            if close > last_high:
                if self.trend == BEAR:
                    self.df.at[self.df.index[i], "choch"] = 1
                else:
                    self.df.at[self.df.index[i], "bos"] = 1
                self.trend = BULL

            elif close < last_low:
                if self.trend == BULL:
                    self.df.at[self.df.index[i], "choch"] = -1
                else:
                    self.df.at[self.df.index[i], "bos"] = -1
                self.trend = BEAR

            self.df.at[self.df.index[i], "trend"] = self.trend

    # ----------------------------
    # FAIR VALUE GAP (FVG)
    # ----------------------------
    def fair_value_gaps(self):
        for i in range(2, len(self.df)):
            high_2 = self.df["high"].iloc[i - 2]
            low_2 = self.df["low"].iloc[i - 2]

            high_0 = self.df["high"].iloc[i]
            low_0 = self.df["low"].iloc[i]

            # bullish FVG
            if low_0 > high_2:
                self.df.at[self.df.index[i], "fvg"] = 1

            # bearish FVG
            elif high_0 < low_2:
                self.df.at[self.df.index[i], "fvg"] = -1

    # ----------------------------
    # ORDER BLOCK (simplified)
    # ----------------------------
    def order_blocks(self):
        self.df["order_block"] = 0

        for i in range(2, len(self.df)):
            if self.df["bos"].iloc[i] == 1:
                # bullish OB = last bearish candle
                for j in range(i - 1, max(0, i - 10), -1):
                    if self.df["close"].iloc[j] < self.df["open"].iloc[j]:
                        self.df.at[self.df.index[j], "order_block"] = 1
                        break

            if self.df["bos"].iloc[i] == -1:
                # bearish OB = last bullish candle
                for j in range(i - 1, max(0, i - 10), -1):
                    if self.df["close"].iloc[j] > self.df["open"].iloc[j]:
                        self.df.at[self.df.index[j], "order_block"] = -1
                        break

    # ----------------------------
    # RUN ALL
    # ----------------------------
    def run(self):
        self.detect_swings()
        self.structure()
        self.fair_value_gaps()
        self.order_blocks()
        return self.df


# -------------------------------------------------------
# SIGNAL GENERATOR
# Reads the last row of a processed SMC DataFrame and
# returns 1 (BUY), -1 (SELL), or 0 (HOLD).
#
# Buy confluence required:
#   • Bullish CHoCH or BOS     (trend shift up)
#   • Bullish FVG              (imbalance to fill up)
#   • Bullish Order Block      (demand zone)
#
# Sell confluence required:
#   • Bearish CHoCH or BOS     (trend shift down)
#   • Bearish FVG              (imbalance to fill down)
#   • Bearish Order Block      (supply zone)
# -------------------------------------------------------
def generate_signal(df: pd.DataFrame) -> int:
    """
    Run the full SMC analysis on df and return the signal for
    the most recent candle.

    Args:
        df: Raw OHLCV DataFrame with columns open, high, low, close.

    Returns:
        1  → BUY  (bullish confluence)
       -1  → SELL (bearish confluence)
        0  → HOLD (no clear signal)
    """
    if len(df) < 15:
        return 0  # Not enough data

    smc = SMCIndicator(df)
    processed = smc.run()
    last = processed.iloc[-1]

    bullish_structure = (last["choch"] == 1) or (last["bos"] == 1)
    bearish_structure = (last["choch"] == -1) or (last["bos"] == -1)
    bullish_fvg = last.get("fvg", 0) == 1
    bearish_fvg = last.get("fvg", 0) == -1
    bullish_ob = last.get("order_block", 0) == 1
    bearish_ob = last.get("order_block", 0) == -1

    # Full confluence: structure + FVG + OB
    if bullish_structure and bullish_fvg and bullish_ob:
        return 1
    if bearish_structure and bearish_fvg and bearish_ob:
        return -1

    # Partial confluence: structure + one confirming factor
    if bullish_structure and (bullish_fvg or bullish_ob):
        return 1
    if bearish_structure and (bearish_fvg or bearish_ob):
        return -1

    return 0