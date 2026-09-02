"""
Smart Money Concepts (SMC) detector for OHLC candle arrays.

The detector is broker-agnostic. It accepts the same OHLC dictionary used by
MT5Connector and DerivConnector and returns structured, JSON-friendly data.
It uses confirmed swing points, so the latest candle is never treated as a
swing until enough candles have formed around it.
"""

from typing import Any, Dict, List, Optional

import numpy as np


class SmartMoneyAnalyzer:
    """Detect BOS, CHOCH, order blocks, FVGs and liquidity events."""

    def __init__(self, config: Optional[dict] = None):
        smc_config = (config or {}).get("smart_money", {})
        self.enabled = smc_config.get("enabled", True)
        self.swing_length = max(2, int(smc_config.get("swing_length", 3)))
        self.equal_tolerance_atr = float(smc_config.get("equal_tolerance_atr", 0.15))
        self.min_confidence = float(smc_config.get("min_confidence", 0.50))
        self.lookback = max(20, int(smc_config.get("lookback", 160)))

    @staticmethod
    def _event(detected: bool = False, direction: str = "none", **extra) -> Dict[str, Any]:
        result = {"detected": bool(detected), "direction": direction,
                  "confidence": float(extra.pop("confidence", 0.0))}
        result.update(extra)
        return result

    def _validate(self, candles: Dict[str, np.ndarray]) -> Optional[Dict[str, np.ndarray]]:
        if not candles:
            return None
        required = ("open", "high", "low", "close")
        if any(key not in candles for key in required):
            return None
        arrays = {key: np.asarray(candles[key], dtype=float) for key in required}
        length = len(arrays["close"])
        if length < max(12, self.swing_length * 4):
            return None
        if any(len(values) != length for values in arrays.values()):
            return None
        if not all(np.isfinite(values).all() for values in arrays.values()):
            return None
        return arrays

    def _atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> float:
        previous_close = np.roll(close, 1)
        true_range = np.maximum(
            high - low,
            np.maximum(np.abs(high - previous_close), np.abs(low - previous_close)),
        )
        true_range[0] = high[0] - low[0]
        window = min(14, len(true_range))
        return float(np.mean(true_range[-window:]))

    def _swings(self, high: np.ndarray, low: np.ndarray) -> Dict[str, List[Dict[str, float]]]:
        radius = self.swing_length
        highs: List[Dict[str, float]] = []
        lows: List[Dict[str, float]] = []
        for index in range(radius, len(high) - radius):
            high_window = high[index - radius:index + radius + 1]
            low_window = low[index - radius:index + radius + 1]
            if high[index] >= np.max(high_window):
                highs.append({"index": index, "price": float(high[index])})
            if low[index] <= np.min(low_window):
                lows.append({"index": index, "price": float(low[index])})
        return {"highs": highs, "lows": lows}

    def _structure(self, close: np.ndarray, swings: Dict[str, List[Dict[str, float]]]) -> Dict[str, Any]:
        highs = swings["highs"]
        lows = swings["lows"]
        if len(highs) < 2 or len(lows) < 2:
            return {"trend": "ranging", "bos": self._event(), "choch": self._event()}

        higher_high = highs[-1]["price"] > highs[-2]["price"]
        higher_low = lows[-1]["price"] > lows[-2]["price"]
        lower_high = highs[-1]["price"] < highs[-2]["price"]
        lower_low = lows[-1]["price"] < lows[-2]["price"]
        trend = "bullish" if higher_high and higher_low else "bearish" if lower_high and lower_low else "ranging"

        last_close = float(close[-1])
        previous_trend = "ranging"
        if len(highs) >= 3 and len(lows) >= 3:
            old_hh = highs[-2]["price"] > highs[-3]["price"]
            old_hl = lows[-2]["price"] > lows[-3]["price"]
            old_lh = highs[-2]["price"] < highs[-3]["price"]
            old_ll = lows[-2]["price"] < lows[-3]["price"]
            previous_trend = "bullish" if old_hh and old_hl else "bearish" if old_lh and old_ll else "ranging"

        bos = self._event()
        choch = self._event()
        if trend == "bullish" and last_close > highs[-1]["price"]:
            bos = self._event(True, "bullish", level=highs[-1]["price"], index=highs[-1]["index"])
        elif trend == "bearish" and last_close < lows[-1]["price"]:
            bos = self._event(True, "bearish", level=lows[-1]["price"], index=lows[-1]["index"])

        if previous_trend == "bearish" and trend == "bullish":
            choch = self._event(True, "bullish", level=highs[-1]["price"], index=highs[-1]["index"])
        elif previous_trend == "bullish" and trend == "bearish":
            choch = self._event(True, "bearish", level=lows[-1]["price"], index=lows[-1]["index"])

        return {"trend": trend, "bos": bos, "choch": choch}

    def _order_block(self, candles: Dict[str, np.ndarray], direction: str, swings: Dict[str, List[Dict[str, float]]]) -> Dict[str, Any]:
        open_, high, low, close = (candles[key] for key in ("open", "high", "low", "close"))
        start = max(0, len(close) - self.lookback)
        for index in range(len(close) - 2, start, -1):
            bullish_candle = close[index] > open_[index]
            bearish_candle = close[index] < open_[index]
            if direction == "bullish" and bearish_candle:
                return {"detected": True, "direction": direction, "index": index, "high": float(high[index]), "low": float(low[index]), "confidence": 70.0}
            if direction == "bearish" and bullish_candle:
                return {"detected": True, "direction": direction, "index": index, "high": float(high[index]), "low": float(low[index]), "confidence": 70.0}
        return {"detected": False, "direction": direction, "confidence": 0.0}

    def _fvg(self, high: np.ndarray, low: np.ndarray) -> Dict[str, Any]:
        for index in range(len(high) - 1, 1, -1):
            if low[index] > high[index - 2]:
                return {"detected": True, "direction": "bullish", "index": index, "low": float(high[index - 2]), "high": float(low[index]), "confidence": 75.0}
            if high[index] < low[index - 2]:
                return {"detected": True, "direction": "bearish", "index": index, "low": float(high[index]), "high": float(low[index - 2]), "confidence": 75.0}
        return {"detected": False, "direction": "none", "confidence": 0.0}

    def _liquidity_sweep(self, close: np.ndarray, high: np.ndarray, low: np.ndarray, swings: Dict[str, List[Dict[str, float]]], atr: float) -> Dict[str, Any]:
        if not swings["highs"] or not swings["lows"]:
            return self._event()
        tolerance = max(atr * 0.10, 1e-12)
        recent_high = swings["highs"][-1]["price"]
        recent_low = swings["lows"][-1]["price"]
        if high[-1] > recent_high + tolerance and close[-1] < recent_high:
            return self._event(True, "bearish", level=recent_high, index=len(close) - 1)
        if low[-1] < recent_low - tolerance and close[-1] > recent_low:
            return self._event(True, "bullish", level=recent_low, index=len(close) - 1)
        return self._event()

    def _equal_levels(self, swings: Dict[str, List[Dict[str, float]]], atr: float) -> Dict[str, Any]:
        tolerance = max(atr * self.equal_tolerance_atr, 1e-12)
        equal_high = equal_low = None
        if len(swings["highs"]) >= 2:
            first, second = swings["highs"][-2:]
            if abs(first["price"] - second["price"]) <= tolerance:
                equal_high = {"detected": True, "level": round((first["price"] + second["price"]) / 2, 8), "indices": [first["index"], second["index"]]}
        if len(swings["lows"]) >= 2:
            first, second = swings["lows"][-2:]
            if abs(first["price"] - second["price"]) <= tolerance:
                equal_low = {"detected": True, "level": round((first["price"] + second["price"]) / 2, 8), "indices": [first["index"], second["index"]]}
        return {"equal_high": equal_high or {"detected": False}, "equal_low": equal_low or {"detected": False}}

    @staticmethod
    def _structure_event(detected: bool, direction: str = "none", confidence: float = 0.0, **extra) -> Dict[str, Any]:
        return {"detected": bool(detected), "direction": direction,
                "confidence": round(max(0.0, min(100.0, float(confidence))), 2), **extra}

    def _advanced_structures(self, data, swings, structure, atr, bullish_ob, bearish_ob, levels):
        close, high, low = data["close"], data["high"], data["low"]
        last_close = float(close[-1])
        highs, lows = swings["highs"], swings["lows"]

        def break_event(point, bullish, confidence):
            if point is None:
                return self._structure_event(False)
            detected = last_close > point["price"] if bullish else last_close < point["price"]
            return self._structure_event(detected, "bullish" if bullish else "bearish", confidence,
                                         level=point["price"], index=point["index"])

        internal_high = highs[-2] if len(highs) > 1 else (highs[-1] if highs else None)
        internal_low = lows[-2] if len(lows) > 1 else (lows[-1] if lows else None)
        external_high = highs[-1] if highs else None
        external_low = lows[-1] if lows else None
        internal_bull = break_event(internal_high, True, 78.0)
        internal_bear = break_event(internal_low, False, 78.0)
        external_bull = break_event(external_high, True, 90.0)
        external_bear = break_event(external_low, False, 90.0)
        internal_bos = internal_bull if internal_bull["detected"] else internal_bear
        external_bos = external_bull if external_bull["detected"] else external_bear

        if structure["bos"].get("detected"):
            structure["bos"]["confidence"] = 90.0 if external_bos["detected"] else 78.0
        if structure["choch"].get("detected"):
            structure["choch"]["confidence"] = 85.0

        mitigation = self._structure_event(False)
        breaker = self._structure_event(False)
        for block, block_direction in ((bullish_ob, "bullish"), (bearish_ob, "bearish")):
            if not block.get("detected"):
                continue
            touched = float(low[-1]) <= block["high"] and float(high[-1]) >= block["low"]
            respected = last_close > block["high"] if block_direction == "bullish" else last_close < block["low"]
            if touched:
                event = self._structure_event(True, block_direction, 72.0, index=block["index"], high=block["high"], low=block["low"])
                if respected:
                    breaker = self._structure_event(True, "bearish" if block_direction == "bullish" else "bullish", 82.0, index=block["index"], high=block["high"], low=block["low"])
                else:
                    mitigation = event

        grab = self._liquidity_sweep(last_close, high, low, swings, atr)
        liquidity_grab = dict(grab)
        liquidity_grab["confidence"] = 88.0 if grab.get("detected") else 0.0
        inducement_direction = "bullish" if internal_bos["direction"] == "bullish" and levels["equal_low"].get("detected") else "bearish" if internal_bos["direction"] == "bearish" and levels["equal_high"].get("detected") else "none"
        inducement = self._structure_event(inducement_direction != "none", inducement_direction, 65.0 if inducement_direction != "none" else 0.0)
        range_high, range_low = max(high[-self.lookback:]), min(low[-self.lookback:])
        midpoint = (range_high + range_low) / 2.0
        premium = self._structure_event(last_close > midpoint, "bearish", 70.0 if last_close > midpoint else 0.0, level=midpoint)
        discount = self._structure_event(last_close < midpoint, "bullish", 70.0 if last_close < midpoint else 0.0, level=midpoint)
        return {"internal_bos": internal_bos, "external_bos": external_bos,
                "mitigation_block": mitigation, "breaker_block": breaker,
                "liquidity_grab": liquidity_grab, "inducement": inducement,
                "premium_zone": premium, "discount_zone": discount}

    def analyze(self, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        empty = {
            "trend_structure": "ranging", "bos": self._event(), "choch": self._event(),
            "bullish_order_block": {"detected": False, "confidence": 0.0}, "bearish_order_block": {"detected": False, "confidence": 0.0},
            "bullish_fvg": {"detected": False, "confidence": 0.0}, "bearish_fvg": {"detected": False, "confidence": 0.0},
            "liquidity_sweep": self._event(), "equal_high": {"detected": False, "confidence": 0.0}, "equal_low": {"detected": False, "confidence": 0.0},
            "premium_discount": {"premium": False, "discount": False, "equilibrium": False}, "confidence": 0.0,
            "internal_bos": self._structure_event(False), "external_bos": self._structure_event(False),
            "mitigation_block": self._structure_event(False), "breaker_block": self._structure_event(False),
            "liquidity_grab": self._structure_event(False), "inducement": self._structure_event(False),
            "premium_zone": self._structure_event(False), "discount_zone": self._structure_event(False),
        }
        if not self.enabled:
            return empty
        data = self._validate(candles)
        if data is None:
            return empty
        high, low, close = data["high"], data["low"], data["close"]
        atr = self._atr(high, low, close)
        swings = self._swings(high, low)
        structure = self._structure(close, swings)
        bullish_ob = self._order_block(data, "bullish", swings)
        bearish_ob = self._order_block(data, "bearish", swings)
        bullish_fvg = {"detected": False, "confidence": 0.0}
        bearish_fvg = {"detected": False, "confidence": 0.0}
        fvg = self._fvg(high, low)
        if fvg["detected"]:
            if fvg["direction"] == "bullish":
                bullish_fvg = fvg
            else:
                bearish_fvg = fvg
        levels = self._equal_levels(swings, atr)
        range_high = max(high[-self.lookback:])
        range_low = min(low[-self.lookback:])
        midpoint = (range_high + range_low) / 2.0
        zones = {"premium": bool(close[-1] > midpoint), "discount": bool(close[-1] < midpoint), "equilibrium": bool(abs(close[-1] - midpoint) <= max(atr * 0.10, 1e-12)), "high": float(range_high), "low": float(range_low), "equilibrium_level": float(midpoint)}
        sweep = self._liquidity_sweep(close, high, low, swings, atr)
        confirmations = sum(bool(item.get("detected")) for item in (structure["bos"], structure["choch"], bullish_ob, bearish_ob, bullish_fvg, bearish_fvg, sweep, levels["equal_high"], levels["equal_low"]))
        confidence = min(1.0, 0.25 + confirmations * 0.08 + (0.12 if structure["trend"] != "ranging" else 0.0))
        advanced = self._advanced_structures(
            data, swings, structure, atr, bullish_ob, bearish_ob, levels
        )
        return {
            "trend_structure": structure["trend"], "bos": structure["bos"], "choch": structure["choch"],
            "bullish_order_block": bullish_ob, "bearish_order_block": bearish_ob,
            "bullish_fvg": bullish_fvg, "bearish_fvg": bearish_fvg, "liquidity_sweep": sweep,
            "equal_high": levels["equal_high"], "equal_low": levels["equal_low"],
            "premium_discount": zones, "confidence": round(confidence, 3),
            **advanced,
        }

    def score(self, result: Dict[str, Any]) -> float:
        """Return a directional SMC score in [-1, 1]."""
        score = {"bullish": 0.0, "bearish": 0.0}
        trend = result.get("trend_structure")
        if trend in score:
            score[trend] += 0.25
        for key in ("bos", "choch", "liquidity_sweep"):
            direction = result.get(key, {}).get("direction")
            if direction in score:
                score[direction] += 0.30 if key != "liquidity_sweep" else 0.20
        if result.get("bullish_order_block", {}).get("detected") or result.get("bullish_fvg", {}).get("detected"):
            score["bullish"] += 0.15
        if result.get("bearish_order_block", {}).get("detected") or result.get("bearish_fvg", {}).get("detected"):
            score["bearish"] += 0.15
        return max(-1.0, min(1.0, score["bullish"] - score["bearish"]))
