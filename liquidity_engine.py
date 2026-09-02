"""Institutional liquidity pool and sweep detector."""

from typing import Any, Dict, List, Optional
import numpy as np


class LiquidityEngine:
    """Find equal highs/lows and allow trading only after a confirmed sweep."""

    def __init__(self, config: Optional[dict] = None):
        settings = (config or {}).get("liquidity_engine", {})
        self.swing_length = max(1, int(settings.get("swing_length", 3)))
        self.equal_tolerance_atr = float(settings.get("equal_tolerance_atr", 0.15))
        self.min_sweep_atr = float(settings.get("min_sweep_atr", 0.10))
        self.lookback = max(20, int(settings.get("lookback", 160)))

    @staticmethod
    def _atr(high, low, close) -> float:
        previous = np.roll(close, 1)
        ranges = np.maximum(high - low, np.maximum(abs(high - previous), abs(low - previous)))
        ranges[0] = high[0] - low[0]
        return float(np.mean(ranges[-min(14, len(ranges)):]))

    def _swings(self, high, low):
        radius = self.swing_length
        highs, lows = [], []
        for index in range(radius, len(high) - radius):
            if high[index] >= np.max(high[index - radius:index + radius + 1]):
                highs.append((index, float(high[index])))
            if low[index] <= np.min(low[index - radius:index + radius + 1]):
                lows.append((index, float(low[index])))
        return highs, lows

    def _pools(self, points, tolerance, side):
        pools: List[Dict[str, Any]] = []
        for first_index in range(len(points)):
            for second_index in range(first_index + 1, len(points)):
                first, second = points[first_index], points[second_index]
                if abs(first[1] - second[1]) <= tolerance:
                    pools.append({"type": side, "level": round((first[1] + second[1]) / 2, 8), "indices": [first[0], second[0]], "confidence": min(100.0, 60.0 + 10.0 * (second_index - first_index))})
        return pools[-10:]

    def analyze(self, candles: Dict[str, Any]) -> Dict[str, Any]:
        required = ("high", "low", "close")
        if not candles or any(key not in candles for key in required):
            return {"liquidity_taken": False, "direction": "none", "confidence": 0.0, "pools": [], "reason": "missing_ohlc"}
        high = np.asarray(candles["high"], dtype=float)
        low = np.asarray(candles["low"], dtype=float)
        close = np.asarray(candles["close"], dtype=float)
        if len(close) < max(10, self.swing_length * 4) or any(len(values) != len(close) for values in (high, low)):
            return {"liquidity_taken": False, "direction": "none", "confidence": 0.0, "pools": [], "reason": "insufficient_history"}
        high, low, close = high[-self.lookback:], low[-self.lookback:], close[-self.lookback:]
        atr = self._atr(high, low, close)
        swing_highs, swing_lows = self._swings(high, low)
        tolerance = max(atr * self.equal_tolerance_atr, 1e-12)
        pools = self._pools(swing_highs, tolerance, "buy_side") + self._pools(swing_lows, tolerance, "sell_side")
        current_high, current_low, current_close = high[-1], low[-1], close[-1]
        threshold = max(atr * self.min_sweep_atr, 1e-12)
        taken = None
        for pool in reversed(pools):
            if pool["type"] == "buy_side" and current_high > pool["level"] + threshold and current_close < pool["level"]:
                taken = ("bearish", pool)
                break
            if pool["type"] == "sell_side" and current_low < pool["level"] - threshold and current_close > pool["level"]:
                taken = ("bullish", pool)
                break
        return {
            "liquidity_taken": taken is not None,
            "direction": taken[0] if taken else "none",
            "confidence": round(min(100.0, (taken[1]["confidence"] + 20.0) if taken else 0.0), 2),
            "pools": pools,
            "taken_pool": taken[1] if taken else None,
            "atr": atr,
            "reason": "liquidity_swept" if taken else "liquidity_not_taken",
        }

    def can_trade(self, candles: Dict[str, Any]) -> bool:
        """Return True only after a liquidity pool has been swept."""
        return bool(self.analyze(candles).get("liquidity_taken"))


LiquidityDetector = LiquidityEngine
