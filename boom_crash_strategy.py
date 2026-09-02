"""Spike probability strategy for Deriv Boom and Crash indices."""

from typing import Any, Dict, Optional, Sequence, Tuple


class BoomCrashStrategy:
    """Detect high-probability directional spikes on Boom and Crash symbols."""

    SIGNALS = ("BUY SPIKE", "SELL SPIKE", "WAIT")

    def __init__(self, config: Optional[dict] = None):
        settings = (config or {}).get("boom_crash_strategy", {})
        self.ema_fast_period = max(2, int(settings.get("ema_fast", 8)))
        self.ema_slow_period = max(self.ema_fast_period + 1, int(settings.get("ema_slow", 34)))
        self.atr_period = max(5, int(settings.get("atr_period", 14)))
        self.rsi_period = max(2, int(settings.get("rsi_period", 14)))
        self.fractal_radius = max(1, int(settings.get("fractal_radius", 2)))
        self.momentum_period = max(2, int(settings.get("momentum_period", 5)))
        self.compression_ratio = float(settings.get("compression_ratio", 0.75))
        self.liquidity_multiplier = float(settings.get("liquidity_multiplier", 1.25))
        self.signal_threshold = float(settings.get("signal_threshold", 90.0))

    @staticmethod
    def _values(candles: Dict[str, Sequence[Any]], key: str):
        return [float(value) for value in candles.get(key, [])]

    @staticmethod
    def _ema(values, period: int) -> float:
        if not values:
            return 0.0
        seed = sum(values[:period]) / min(period, len(values))
        multiplier = 2.0 / (period + 1)
        result = seed
        for value in values[period:]:
            result = (value - result) * multiplier + result
        return result

    def _atr(self, highs, lows, closes) -> Tuple[float, float]:
        true_ranges = []
        for index, high in enumerate(highs):
            previous = closes[index - 1] if index else closes[index]
            true_ranges.append(max(high - lows[index], abs(high - previous), abs(lows[index] - previous)))
        current = sum(true_ranges[-self.atr_period:]) / min(self.atr_period, len(true_ranges))
        baseline_window = min(len(true_ranges), self.atr_period * 3)
        baseline = sum(true_ranges[-baseline_window:]) / baseline_window
        return current, baseline

    def _rsi(self, closes) -> float:
        changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
        window = changes[-self.rsi_period:]
        gains = [max(change, 0.0) for change in window]
        losses = [max(-change, 0.0) for change in window]
        average_gain = sum(gains) / len(window) if window else 0.0
        average_loss = sum(losses) / len(window) if window else 0.0
        if average_loss == 0:
            return 100.0 if average_gain else 50.0
        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    def _fractals(self, highs, lows) -> Dict[str, Optional[float]]:
        radius = self.fractal_radius
        high_points = []
        low_points = []
        last_confirmed = len(highs) - radius
        for index in range(radius, last_confirmed):
            high_window = highs[index - radius:index + radius + 1]
            low_window = lows[index - radius:index + radius + 1]
            if highs[index] >= max(high_window):
                high_points.append(highs[index])
            if lows[index] <= min(low_window):
                low_points.append(lows[index])
        return {
            "high": high_points[-1] if high_points else None,
            "low": low_points[-1] if low_points else None,
        }

    def _validate(self, symbol: str, candles: Dict[str, Sequence[Any]]) -> Tuple[bool, str, str]:
        upper_symbol = str(symbol).upper()
        if not (upper_symbol.startswith("BOOM") or upper_symbol.startswith("CRASH")):
            return False, "unsupported_symbol", ""
        required = ("open", "high", "low", "close")
        if any(key not in candles for key in required):
            return False, "missing_ohlc", ""
        length = len(candles["close"])
        minimum = max(self.ema_slow_period + 2, self.atr_period * 2, self.rsi_period + 2, 30)
        if length < minimum:
            return False, "insufficient_history", ""
        if any(len(candles[key]) != length for key in required):
            return False, "inconsistent_ohlc", ""
        return True, "ok", "BOOM" if upper_symbol.startswith("BOOM") else "CRASH"

    def analyze(self, symbol: str, candles: Dict[str, Sequence[Any]]) -> Dict[str, Any]:
        """Return BUY SPIKE, SELL SPIKE, or WAIT with a probability."""
        valid, reason, family = self._validate(symbol, candles)
        if not valid:
            return {"signal": "WAIT", "confidence": 0.0, "probability": 0.0, "reason": reason}

        highs = self._values(candles, "high")
        lows = self._values(candles, "low")
        closes = self._values(candles, "close")
        volumes = self._values(candles, "volume") or self._values(candles, "tick_volume")
        current = closes[-1]
        atr, baseline_atr = self._atr(highs, lows, closes)
        fast_ema = self._ema(closes, self.ema_fast_period)
        slow_ema = self._ema(closes, self.ema_slow_period)
        rsi = self._rsi(closes)
        momentum = (current - closes[-self.momentum_period - 1]) / max(atr, 1e-12)
        fractals = self._fractals(highs, lows)
        average_range = sum(high - low for high, low in zip(highs[-20:], lows[-20:])) / min(20, len(highs))
        long_range_window = min(len(highs), self.atr_period * 3)
        long_range = sum(high - low for high, low in zip(highs[-long_range_window:], lows[-long_range_window:])) / long_range_window
        compressed = average_range <= long_range * self.compression_ratio
        average_volume = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0.0
        liquid = bool(volumes and average_volume > 0 and volumes[-1] >= average_volume * self.liquidity_multiplier)
        expansion = atr >= baseline_atr * 1.05

        bullish_fractal_break = fractals["high"] is not None and current > fractals["high"]
        bearish_fractal_break = fractals["low"] is not None and current < fractals["low"]
        bullish = [fast_ema > slow_ema, momentum > 1.0, rsi >= 55, bullish_fractal_break, compressed or expansion, liquid]
        bearish = [fast_ema < slow_ema, momentum < -1.0, rsi <= 45, bearish_fractal_break, compressed or expansion, liquid]
        bullish_score = sum(bullish) / len(bullish)
        bearish_score = sum(bearish) / len(bearish)
        directional_score = bullish_score if family == "BOOM" else bearish_score
        opposite_score = bearish_score if family == "BOOM" else bullish_score
        probability = 50.0 + directional_score * 50.0 - opposite_score * 15.0
        signal = "BUY SPIKE" if family == "BOOM" else "SELL SPIKE"
        if directional_score < 1.0 or directional_score <= opposite_score or probability <= self.signal_threshold:
            signal = "WAIT"
        probability = max(0.0, min(99.0, probability))
        return {
            "signal": signal,
            "confidence": round(probability, 2),
            "probability": round(probability, 2),
            "symbol_family": family,
            "features": {
                "ema_fast": fast_ema,
                "ema_slow": slow_ema,
                "atr": atr,
                "atr_baseline": baseline_atr,
                "rsi": rsi,
                "momentum_atr": momentum,
                "fractals": fractals,
                "compression": compressed,
                "volatility_expansion": expansion,
                "liquidity_confirmed": liquid,
            },
        }

    generate_signal = analyze


BoomCrashAI = BoomCrashStrategy
