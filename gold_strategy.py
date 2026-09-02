"""XAUUSDm strategy for Exness.

This module is broker-independent and consumes OHLCV candles. It returns only
BUY, SELL, or NO TRADE, together with a confidence percentage.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class GoldStrategy:
    """Session and market-structure strategy specialized for Exness gold."""

    SYMBOL = "XAUUSDm"
    SIGNALS = ("BUY", "SELL", "NO TRADE")

    def __init__(self, config: Optional[dict] = None):
        settings = (config or {}).get("gold_strategy", {})
        self.session_windows = settings.get(
            "session_windows", {"london": (7, 12), "new_york": (13, 21)}
        )
        self.asian_start = int(settings.get("asian_start_utc", 0))
        self.asian_end = int(settings.get("asian_end_utc", 7))
        self.atr_period = max(5, int(settings.get("atr_period", 14)))
        self.atr_sl_multiplier = float(settings.get("atr_sl_multiplier", 1.8))
        self.volume_multiplier = float(settings.get("volume_multiplier", 1.15))
        self.profile_bins = max(10, int(settings.get("volume_profile_bins", 24)))
        self.min_confidence = float(settings.get("min_confidence", 60.0))
        self.news_blackout_minutes = int(settings.get("news_blackout_minutes", 60))

    @staticmethod
    def _values(candles: Dict[str, Sequence[Any]], key: str) -> List[float]:
        return [float(value) for value in candles.get(key, [])]

    @staticmethod
    def _timestamp(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _timestamps(candles: Dict[str, Sequence[Any]]) -> Sequence[Any]:
        values = candles.get("time")
        if values is not None and len(values) > 0:
            return values
        values = candles.get("timestamp")
        return values if values is not None else []

    def _validate(self, symbol: str, candles: Dict[str, Sequence[Any]]) -> Tuple[bool, str]:
        if symbol != self.SYMBOL:
            return False, "symbol_not_supported"
        required = ("open", "high", "low", "close")
        if any(key not in candles for key in required):
            return False, "missing_ohlc"
        length = len(candles["close"])
        if length < max(self.atr_period + 2, 30):
            return False, "insufficient_history"
        if any(len(candles[key]) != length for key in required):
            return False, "inconsistent_ohlc"
        return True, "ok"

    def _atr(self, candles: Dict[str, Sequence[Any]]) -> float:
        highs = self._values(candles, "high")
        lows = self._values(candles, "low")
        closes = self._values(candles, "close")
        ranges = []
        for index, high in enumerate(highs):
            previous = closes[index - 1] if index else closes[index]
            ranges.append(max(high - lows[index], abs(high - previous), abs(lows[index] - previous)))
        return sum(ranges[-self.atr_period:]) / min(len(ranges), self.atr_period)

    def _vwap(self, candles: Dict[str, Sequence[Any]]) -> float:
        highs = self._values(candles, "high")
        lows = self._values(candles, "low")
        closes = self._values(candles, "close")
        volumes = self._values(candles, "volume") or self._values(candles, "tick_volume")
        if not volumes or len(volumes) != len(closes) or sum(volumes) <= 0:
            return sum(closes[-self.atr_period:]) / min(len(closes), self.atr_period)
        typical = [(high + low + close) / 3 for high, low, close in zip(highs, lows, closes)]
        return sum(price * volume for price, volume in zip(typical, volumes)) / sum(volumes)

    def volume_profile(self, candles: Dict[str, Sequence[Any]]) -> Dict[str, float]:
        """Return POC and value area using a close-price volume distribution."""
        highs = self._values(candles, "high")
        lows = self._values(candles, "low")
        closes = self._values(candles, "close")
        volumes = self._values(candles, "volume") or self._values(candles, "tick_volume")
        if not volumes or len(volumes) != len(closes):
            volumes = [1.0] * len(closes)
        low, high = min(lows), max(highs)
        width = (high - low) / self.profile_bins if high > low else 1.0
        distribution = [0.0] * self.profile_bins
        for price, volume in zip(closes, volumes):
            index = min(self.profile_bins - 1, max(0, int((price - low) / width)))
            distribution[index] += max(0.0, volume)
        poc_index = max(range(self.profile_bins), key=distribution.__getitem__)
        total = sum(distribution)
        target = total * 0.70
        selected = {poc_index}
        covered = distribution[poc_index]
        while covered < target and len(selected) < self.profile_bins:
            candidates = [index for index in (min(selected) - 1, max(selected) + 1) if 0 <= index < self.profile_bins and index not in selected]
            if not candidates:
                break
            index = max(candidates, key=distribution.__getitem__)
            selected.add(index)
            covered += distribution[index]
        return {
            "poc": low + (poc_index + 0.5) * width,
            "value_area_low": low + min(selected) * width,
            "value_area_high": low + (max(selected) + 1) * width,
        }

    def _session(self, timestamp: Optional[Any]) -> Optional[str]:
        current = self._timestamp(timestamp)
        if current is None:
            return None
        hour = current.hour + current.minute / 60
        for name, window in self.session_windows.items():
            start, end = float(window[0]), float(window[1])
            if start <= hour < end:
                return name
        return None

    def _asian_range(self, candles: Dict[str, Sequence[Any]]) -> Optional[Tuple[float, float]]:
        timestamps = self._timestamps(candles)
        if len(timestamps) == 0:
            return None
        highs = self._values(candles, "high")
        lows = self._values(candles, "low")
        asian_highs, asian_lows = [], []
        for timestamp, high, low in zip(timestamps, highs, lows):
            current = self._timestamp(timestamp)
            if current is not None and self.asian_start <= current.hour < self.asian_end:
                asian_highs.append(high)
                asian_lows.append(low)
        if not asian_highs:
            return None
        return max(asian_highs), min(asian_lows)

    def _news_blocked(self, news_events: Optional[Iterable[Any]], timestamp: Optional[Any]) -> Optional[str]:
        current = self._timestamp(timestamp)
        if current is None:
            return None
        for event in news_events or []:
            if isinstance(event, str):
                name, event_time = event, current
            else:
                name = str(event.get("name", event.get("type", ""))).upper()
                event_time = self._timestamp(event.get("time", event.get("timestamp")))
            if not event_time or abs((current - event_time).total_seconds()) <= self.news_blackout_minutes * 60:
                if any(keyword in name for keyword in ("CPI", "FOMC", "NFP", "NON-FARM", "PAYROLL")):
                    return name or "economic_news"
        return None

    def _fake_breakout(self, close: float, high: float, low: float, asian_range: Optional[Tuple[float, float]]) -> Optional[str]:
        if not asian_range:
            return None
        asian_high, asian_low = asian_range
        if high > asian_high and close <= asian_high:
            return "bearish"
        if low < asian_low and close >= asian_low:
            return "bullish"
        return None

    def analyze(
        self,
        candles: Dict[str, Sequence[Any]],
        symbol: str = SYMBOL,
        timestamp: Optional[Any] = None,
        news_events: Optional[Iterable[Any]] = None,
    ) -> Dict[str, Any]:
        """Return a BUY, SELL, or NO TRADE signal with confidence."""
        valid, reason = self._validate(symbol, candles)
        if not valid:
            return {"signal": "NO TRADE", "confidence": 0.0, "reason": reason}
        timestamps = self._timestamps(candles)
        current_time = timestamp if timestamp is not None else (timestamps[-1] if len(timestamps) else None)
        session = self._session(current_time)
        news = self._news_blocked(news_events, current_time)
        if not session:
            return {"signal": "NO TRADE", "confidence": 0.0, "reason": "outside_london_new_york"}
        if news:
            return {"signal": "NO TRADE", "confidence": 0.0, "reason": "news_filter:" + news, "session": session}

        closes = self._values(candles, "close")
        highs = self._values(candles, "high")
        lows = self._values(candles, "low")
        volumes = self._values(candles, "volume") or self._values(candles, "tick_volume")
        close, high, low = closes[-1], highs[-1], lows[-1]
        atr = self._atr(candles)
        vwap = self._vwap(candles)
        profile = self.volume_profile(candles)
        asian_range = self._asian_range(candles)
        fake = self._fake_breakout(close, high, low, asian_range)
        average_volume = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0.0
        volume_confirmed = bool(volumes and average_volume > 0 and volumes[-1] >= average_volume * self.volume_multiplier)
        breakout = None
        if asian_range and close > asian_range[0] + atr * 0.05:
            breakout = "bullish"
        elif asian_range and close < asian_range[1] - atr * 0.05:
            breakout = "bearish"
        bullish_points = int(close > vwap) + int(close > profile["poc"]) + int(volume_confirmed) + int(breakout == "bullish") + int(fake == "bullish")
        bearish_points = int(close < vwap) + int(close < profile["poc"]) + int(volume_confirmed) + int(breakout == "bearish") + int(fake == "bearish")
        if fake == "bullish":
            bullish_points += 1
        if fake == "bearish":
            bearish_points += 1
        points = max(bullish_points, bearish_points)
        direction = "BUY" if bullish_points > bearish_points else "SELL" if bearish_points > bullish_points else "NO TRADE"
        confidence = min(95.0, 45.0 + points * 10.0 + (5.0 if breakout else 0.0))
        if direction == "NO TRADE" or confidence < self.min_confidence:
            direction = "NO TRADE"
        return {
            "signal": direction,
            "confidence": round(confidence if direction != "NO TRADE" else min(confidence, 49.0), 2),
            "session": session,
            "atr": round(atr, 5),
            "vwap": round(vwap, 5),
            "volume_profile": profile,
            "asian_range": asian_range,
            "breakout": breakout,
            "fake_breakout": fake,
            "volume_confirmed": volume_confirmed,
            "sl_distance": round(atr * self.atr_sl_multiplier, 5),
        }

    generate_signal = analyze


GoldStrategyXAUUSDm = GoldStrategy
