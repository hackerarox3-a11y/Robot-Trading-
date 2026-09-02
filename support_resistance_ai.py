"""Adaptive support, resistance and supply/demand zones."""
from typing import Any, Dict, Sequence


class SupportResistanceAI:
    def __init__(self, lookback: int = 120, tolerance: float = 0.002):
        self.lookback = max(20, int(lookback))
        self.tolerance = float(tolerance)

    def analyze(self, candles: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        high, low, close = [list(map(float, candles[key]))[-self.lookback:] for key in ("high", "low", "close")]
        if not close:
            return {"support": [], "resistance": [], "supply_zones": [], "demand_zones": [], "confidence": 0.0}
        supports, resistances = [], []
        for i in range(2, len(close) - 2):
            if low[i] <= min(low[i-2:i+3]): supports.append(low[i])
            if high[i] >= max(high[i-2:i+3]): resistances.append(high[i])
        atr = sum(h - l for h, l in zip(high[-14:], low[-14:])) / min(14, len(close))
        zones = {"support": sorted(set(supports), reverse=True)[:5], "resistance": sorted(set(resistances))[:5],
                 "dynamic_support": sum(close[-min(20, len(close)):]) / min(20, len(close)) - atr,
                 "dynamic_resistance": sum(close[-min(20, len(close)):]) / min(20, len(close)) + atr,
                 "supply_zones": [{"low": h - atr * .25, "high": h, "confidence": 70.0} for h in resistances[-3:]],
                 "demand_zones": [{"low": l, "high": l + atr * .25, "confidence": 70.0} for l in supports[-3:]]}
        zones["confidence"] = min(100.0, 40.0 + (len(supports) + len(resistances)) * 3.0)
        return zones


SRAnalysis = SupportResistanceAI
