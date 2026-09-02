"""Automatic market structure map built from confirmed swing points."""
from typing import Any, Dict, Sequence


class MarketStructureAI:
    def __init__(self, swing_length: int = 3):
        self.swing_length = max(1, int(swing_length))

    def analyze(self, candles: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        high, low, close = candles["high"], candles["low"], candles["close"]
        radius = self.swing_length
        highs, lows = [], []
        for i in range(radius, len(close) - radius):
            if high[i] >= max(high[i-radius:i+radius+1]): highs.append((i, float(high[i])))
            if low[i] <= min(low[i-radius:i+radius+1]): lows.append((i, float(low[i])))
        trend = "ranging"
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]: trend = "bullish"
            elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]: trend = "bearish"
        return {"trend": trend, "swing_highs": highs, "swing_lows": lows,
                "current_price": float(close[-1]), "map_confidence": min(100.0, 40.0 + 10.0 * min(len(highs), len(lows)))}


StructureAI = MarketStructureAI
