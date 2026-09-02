"""Institutional VWAP levels: daily, weekly, monthly and anchored."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence


class InstitutionalVWAP:
    """Compute time-anchored VWAP levels from OHLCV candles."""

    @staticmethod
    def _vwap(high, low, close, volume, indices):
        if not indices:
            return None
        weights = [max(0.0, float(volume[i])) for i in indices]
        total = sum(weights)
        prices = [(float(high[i]) + float(low[i]) + float(close[i])) / 3 for i in indices]
        return sum(p * w for p, w in zip(prices, weights)) / total if total else sum(prices) / len(prices)

    @staticmethod
    def calculate(data: Dict[str, Sequence[Any]], anchor: Optional[int] = None) -> Dict[str, Optional[float]]:
        high, low, close = data["high"], data["low"], data["close"]
        volume = data.get("volume", data.get("tick_volume", [1.0] * len(close)))
        times = data.get("time", data.get("timestamp", []))
        groups = {"daily": [], "weekly": [], "monthly": []}
        if len(times):
            for index, value in enumerate(times):
                current = value if isinstance(value, datetime) else datetime.fromtimestamp(float(value), timezone.utc)
                groups["daily"].append(index)
                if current.isocalendar().week == datetime.now(timezone.utc).isocalendar().week:
                    groups["weekly"].append(index)
                if current.year == datetime.now(timezone.utc).year and current.month == datetime.now(timezone.utc).month:
                    groups["monthly"].append(index)
        else:
            groups = {"daily": list(range(min(len(close), 24))), "weekly": list(range(min(len(close), 120))), "monthly": list(range(len(close)))}
        if anchor is not None:
            groups["anchored"] = list(range(max(0, int(anchor)), len(close)))
        result = {name: InstitutionalVWAP._vwap(high, low, close, volume, indices) for name, indices in groups.items()}
        result["anchored"] = InstitutionalVWAP._vwap(high, low, close, volume, groups.get("anchored", list(range(len(close)))))
        return result

    def analyze(self, data: Dict[str, Sequence[Any]], anchor: Optional[int] = None):
        return self.calculate(data, anchor)


VWAPEngine = InstitutionalVWAP
