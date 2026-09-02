"""Hierarchical multi-timeframe analysis for MT5 and compatible connectors."""

import logging
import threading
from typing import Any, Dict, Optional

import numpy as np

from technical_analysis import TechnicalAnalysis

logger = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400}


class TimeframeAnalyzer:
    """Analyze M1/M5/M15/H1/H4 using a top-down trading hierarchy."""

    def __init__(self, connector, config: dict):
        self.connector = connector
        self.config = config
        settings = config.get("timeframe_analyzer", {})
        self.enabled = bool(settings.get("enabled", True))
        self.bars = int(settings.get("bars_per_timeframe", 300))
        self._lock = threading.RLock()

    def _get_data(self, symbol: str, timeframe: str) -> Optional[Dict[str, np.ndarray]]:
        """Read a timeframe without leaving the connector on a changed timeframe."""
        if hasattr(self.connector, "get_ohlc_data_for_timeframe"):
            return self.connector.get_ohlc_data_for_timeframe(symbol, timeframe, self.bars)
        if not hasattr(self.connector, "get_ohlc_data"):
            return None
        with self._lock:
            original_tf = getattr(self.connector, "timeframe_str", None)
            original_seconds = getattr(self.connector, "timeframe_seconds", None)
            original_timeframe = getattr(self.connector, "timeframe", None)
            try:
                if hasattr(self.connector, "timeframe_str"):
                    self.connector.timeframe_str = timeframe
                if hasattr(self.connector, "timeframe"):
                    from mt5_connector import TIMEFRAME_MAP
                    self.connector.timeframe = TIMEFRAME_MAP.get(timeframe, self.connector.timeframe)
                if hasattr(self.connector, "timeframe_seconds"):
                    self.connector.timeframe_seconds = TIMEFRAME_SECONDS[timeframe]
                return self.connector.get_ohlc_data(symbol)
            except Exception as error:
                logger.debug("Timeframe %s/%s indisponible: %s", symbol, timeframe, error)
                return None
            finally:
                if original_tf is not None:
                    self.connector.timeframe_str = original_tf
                if original_seconds is not None:
                    self.connector.timeframe_seconds = original_seconds
                if original_timeframe is not None:
                    self.connector.timeframe = original_timeframe

    @staticmethod
    def _latest(values: Any) -> Optional[float]:
        if isinstance(values, np.ndarray):
            valid = values[np.isfinite(values)]
            return float(valid[-1]) if len(valid) else None
        return None

    @staticmethod
    def _structure(data: Dict[str, np.ndarray]) -> Dict[str, Any]:
        high = data["high"]
        low = data["low"]
        if len(high) < 6:
            return {"label": "ranging", "direction": "none"}
        recent_high = float(np.max(high[-3:]))
        previous_high = float(np.max(high[-6:-3]))
        recent_low = float(np.min(low[-3:]))
        previous_low = float(np.min(low[-6:-3]))
        if recent_high > previous_high and recent_low > previous_low:
            return {"label": "HH_HL", "direction": "bullish"}
        if recent_high < previous_high and recent_low < previous_low:
            return {"label": "LH_LL", "direction": "bearish"}
        return {"label": "ranging", "direction": "none"}

    def analyze_timeframe(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        """Return indicators and market structure for one symbol/timeframe."""
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("Unsupported timeframe: %s" % timeframe)
        data = self._get_data(symbol, timeframe)
        if data is None or len(data.get("close", [])) < 30:
            return {"timeframe": timeframe, "available": False, "direction": "none"}
        technical = TechnicalAnalysis(self.config)
        analysis = technical.full_analysis(data)
        latest = technical.get_latest_values(analysis)
        close = float(data["close"][-1])
        ema_fast = latest.get("ema_fast")
        ema_slow = latest.get("ema_slow")
        macd = latest.get("macd_line")
        macd_signal = latest.get("macd_signal")
        bullish_votes = sum((
            ema_fast is not None and ema_slow is not None and ema_fast > ema_slow,
            macd is not None and macd_signal is not None and macd > macd_signal,
            close > (latest.get("ema_trend") or close),
        ))
        bearish_votes = sum((
            ema_fast is not None and ema_slow is not None and ema_fast < ema_slow,
            macd is not None and macd_signal is not None and macd < macd_signal,
            close < (latest.get("ema_trend") or close),
        ))
        direction = "bullish" if bullish_votes >= 2 else "bearish" if bearish_votes >= 2 else "none"
        return {
            "timeframe": timeframe,
            "available": True,
            "direction": direction,
            "ema_trend": "bullish" if ema_fast is not None and ema_slow is not None and ema_fast > ema_slow else "bearish",
            "ema_fast": self._latest(analysis.get("ema_fast")),
            "ema_slow": self._latest(analysis.get("ema_slow")),
            "rsi": latest.get("rsi"),
            "macd": latest.get("macd_line"),
            "macd_signal": latest.get("macd_signal"),
            "adx": latest.get("adx"),
            "volume": float(data.get("tick_volume", data.get("volume", np.array([0])))[-1]),
            "volume_average": float(np.mean(data.get("tick_volume", data.get("volume", np.array([0])))[-20:])),
            "atr": latest.get("atr"),
            "ichimoku": {
                "tenkan": latest.get("ichimoku_tenkan"),
                "kijun": latest.get("ichimoku_kijun"),
                "senkou_a": latest.get("ichimoku_senkou_a"),
                "senkou_b": latest.get("ichimoku_senkou_b"),
            },
            "market_structure": self._structure(data),
        }

    def combine_timeframes(self, symbol: str, m5_signal: str = "HOLD") -> Dict[str, Any]:
        """Combine the top-down hierarchy and enforce H4/H1 alignment."""
        if not self.enabled:
            return {"allowed": True, "direction": m5_signal, "h4_h1_aligned": True, "timeframes": {}, "rules": {}}
        results = {tf: self.analyze_timeframe(symbol, tf) for tf in ("M1", "M5", "M15", "H1", "H4")}
        h4 = results["H4"].get("direction", "none")
        h1 = results["H1"].get("direction", "none")
        higher_aligned = h4 in ("bullish", "bearish") and h4 == h1
        expected = "bullish" if m5_signal == "BUY" else "bearish" if m5_signal == "SELL" else "none"
        m15_ok = results["M15"].get("direction", "none") in (expected, "none")
        m1_ok = results["M1"].get("direction", "none") in (expected, "none")
        allowed = bool(m5_signal in ("BUY", "SELL") and higher_aligned and m15_ok and m1_ok)
        direction = "BUY" if higher_aligned and h4 == "bullish" else "SELL" if higher_aligned and h4 == "bearish" else "HOLD"
        return {
            "allowed": allowed,
            "direction": direction,
            "h4_h1_aligned": higher_aligned,
            "timeframes": results,
            "rules": {"h4": h4, "h1": h1, "m15_valid": m15_ok, "m5_signal": m5_signal, "m1_entry_ok": m1_ok},
        }
