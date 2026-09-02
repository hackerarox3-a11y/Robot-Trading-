"""AI-style deterministic divergence detector for RSI, MACD, volume and delta."""
from typing import Any, Dict, Sequence


class DivergenceAI:
    def __init__(self, lookback: int = 40):
        self.lookback = max(10, int(lookback))

    @staticmethod
    def _divergence(price, indicator):
        if len(price) < 6 or len(indicator) != len(price): return {"detected": False, "direction": "none", "confidence": 0.0}
        half = len(price) // 2
        p1, p2 = min(price[:half]), min(price[half:])
        i1, i2 = min(indicator[:half]), min(indicator[half:])
        if p2 < p1 and i2 > i1: return {"detected": True, "direction": "bullish", "confidence": 75.0}
        p1, p2 = max(price[:half]), max(price[half:])
        i1, i2 = max(indicator[:half]), max(indicator[half:])
        if p2 > p1 and i2 < i1: return {"detected": True, "direction": "bearish", "confidence": 75.0}
        return {"detected": False, "direction": "none", "confidence": 0.0}

    def analyze(self, data: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        price = list(map(float, data.get("close", [])))[-self.lookback:]
        result = {}
        for name, key in (("rsi", "rsi"), ("macd", "macd"), ("volume", "volume"), ("cumulative_delta", "cumulative_delta")):
            values = data.get(key)
            if values is None and key == "volume": values = data.get("tick_volume")
            result[name] = self._divergence(price, list(map(float, values[-len(price):]))) if values is not None and len(values) >= len(price) else {"detected": False, "direction": "none", "confidence": 0.0}
        detected = [item for item in result.values() if item["detected"]]
        result["overall"] = {"detected": bool(detected), "direction": max(set(item["direction"] for item in detected), key=lambda direction: sum(item["direction"] == direction for item in detected)) if detected else "none", "confidence": min(100.0, sum(item["confidence"] for item in detected) / max(1, len(detected)) + len(detected) * 5)}
        return result


DivergenceEngine = DivergenceAI
