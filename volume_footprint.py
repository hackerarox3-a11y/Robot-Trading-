"""OHLCV volume footprint approximations and tick-level delta support."""
from typing import Any, Dict, Sequence


class VolumeFootprint:
    """Calculate delta, cumulative delta, absorption and aggressor imbalance."""

    @staticmethod
    def _calculate(data: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        buy = data.get("aggressive_buy_volume", data.get("buy_volume"))
        sell = data.get("aggressive_sell_volume", data.get("sell_volume"))
        volume = data.get("volume", data.get("tick_volume", []))
        close, open_ = data.get("close", []), data.get("open", [])
        if buy is None or sell is None:
            buy, sell = [], []
            for index, total in enumerate(volume):
                body = float(close[index]) - float(open_[index])
                buy.append(float(total) * (0.5 + 0.5 if body > 0 else 0.5 - 0.5 if body < 0 else 0.0))
                sell.append(float(total) - buy[-1])
        delta = [float(b) - float(s) for b, s in zip(buy, sell)]
        cumulative = []
        running = 0.0
        for value in delta:
            running += value
            cumulative.append(running)
        avg_volume = sum(float(v) for v in volume[-20:]) / max(1, min(20, len(volume))) if len(volume) else 0.0
        absorption = [abs(d) < max(float(v) * 0.10, 1e-12) and float(v) >= avg_volume * 1.5 for d, v in zip(delta, volume)]
        return {
            "delta_volume": delta, "cumulative_delta": cumulative,
            "aggressive_buyers": [float(v) for v in buy],
            "aggressive_sellers": [float(v) for v in sell],
            "absorption": absorption,
            "latest": {
                "delta_volume": delta[-1] if delta else 0.0,
                "cumulative_delta": cumulative[-1] if cumulative else 0.0,
                "aggressive_buyers": float(buy[-1]) if buy else 0.0,
                "aggressive_sellers": float(sell[-1]) if sell else 0.0,
                "absorption": bool(absorption[-1]) if absorption else False,
                "data_source": "ticks" if data.get("buy_volume") is not None or data.get("aggressive_buy_volume") is not None else "ohlcv_estimate",
            },
        }

    def analyze(self, data: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        return self._calculate(data)


VolumeFootprintEngine = VolumeFootprint
