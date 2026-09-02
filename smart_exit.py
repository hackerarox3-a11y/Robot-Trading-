"""Smart SL/TP management for MT5 positions.

The module is broker-aware at execution time but keeps level calculation
independent from MetaTrader5, which makes it safe to test without a terminal.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

from smart_money import SmartMoneyAnalyzer

logger = logging.getLogger(__name__)

BUY = 0
SELL = 1


class SmartExitManager:
    """Build and manage structure-based exits for MT5 positions."""

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        exit_config = config.get("smart_exit", {})
        self.swing_length = max(1, int(exit_config.get("swing_length", 3)))
        self.atr_period = max(1, int(exit_config.get("atr_period", 14)))
        self.atr_sl_multiplier = float(exit_config.get("atr_sl_multiplier", 1.5))
        self.atr_trailing_multiplier = float(
            exit_config.get("atr_trailing_multiplier", 2.0)
        )
        self.tp_multipliers = tuple(
            float(value) for value in exit_config.get("tp_multipliers", (1.0, 2.0, 3.0))
        )
        if len(self.tp_multipliers) != 3 or any(value <= 0 for value in self.tp_multipliers):
            raise ValueError("tp_multipliers doit contenir trois valeurs positives")
        self.tp_portions = (0.30, 0.30, 0.40)
        self.smc = SmartMoneyAnalyzer(config)

    def calculate_atr(self, candles: Dict[str, Sequence[float]]) -> float:
        """Calculate the latest Wilder-compatible simple ATR value."""
        highs = [float(value) for value in candles["high"]]
        lows = [float(value) for value in candles["low"]]
        closes = [float(value) for value in candles["close"]]
        if not highs or len(highs) != len(lows) or len(highs) != len(closes):
            return 0.0
        true_ranges: List[float] = []
        for index, high in enumerate(highs):
            previous_close = closes[index - 1] if index else closes[index]
            true_ranges.append(max(
                high - lows[index],
                abs(high - previous_close),
                abs(lows[index] - previous_close),
            ))
        return sum(true_ranges[-self.atr_period:]) / min(self.atr_period, len(true_ranges))

    def _confirmed_swings(self, candles: Dict[str, Sequence[float]]) -> Dict[str, List[float]]:
        highs = [float(value) for value in candles["high"]]
        lows = [float(value) for value in candles["low"]]
        radius = self.swing_length
        swing_highs: List[float] = []
        swing_lows: List[float] = []
        for index in range(radius, len(highs) - radius):
            if highs[index] >= max(highs[index - radius:index + radius + 1]):
                swing_highs.append(highs[index])
            if lows[index] <= min(lows[index - radius:index + radius + 1]):
                swing_lows.append(lows[index])
        return {"highs": swing_highs, "lows": swing_lows}

    @staticmethod
    def _structure_levels(smc_result: Dict[str, Any], direction: str) -> List[float]:
        bullish = direction == "BUY"
        key_prefix = "bullish" if bullish else "bearish"
        levels: List[float] = []
        order_block = smc_result.get(key_prefix + "_order_block", {})
        fvg = smc_result.get(key_prefix + "_fvg", {})
        if order_block.get("detected"):
            levels.append(float(order_block.get("low" if bullish else "high")))
        if fvg.get("detected"):
            levels.append(float(fvg.get("low" if bullish else "high")))
        return levels

    def calculate_exit_levels(
        self,
        position: Dict[str, Any],
        candles: Optional[Dict[str, Sequence[float]]] = None,
        smc_result: Optional[Dict[str, Any]] = None,
        atr_value: Optional[float] = None,
        digits: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return structure/ATR SL, three take-profits and TP portions."""
        direction = "BUY" if position.get("type", BUY) == BUY else "SELL"
        entry = float(position.get("price_open", position.get("entry_price", 0.0)))
        if entry <= 0:
            raise ValueError("position doit contenir un prix d'ouverture positif")
        if candles is not None and smc_result is None:
            smc_result = self.smc.analyze(candles)
        smc_result = smc_result or {}
        atr = float(atr_value or (self.calculate_atr(candles) if candles else 0.0))
        structure = self._structure_levels(smc_result, direction)
        if candles:
            swings = self._confirmed_swings(candles)
            structure.extend(swings["lows" if direction == "BUY" else "highs"][-3:])
        atr_stop = entry - atr * self.atr_sl_multiplier if direction == "BUY" else entry + atr * self.atr_sl_multiplier
        valid = [level for level in structure if level < entry] if direction == "BUY" else [level for level in structure if level > entry]
        stop = max(valid + [atr_stop]) if direction == "BUY" else min(valid + [atr_stop])
        if direction == "BUY":
            stop = min(stop, entry - max(atr * 0.05, 1e-12))
        else:
            stop = max(stop, entry + max(atr * 0.05, 1e-12))
        risk = abs(entry - stop)
        take_profits = [entry + risk * multiplier if direction == "BUY" else entry - risk * multiplier for multiplier in self.tp_multipliers]
        if digits is not None:
            stop = round(stop, digits)
            take_profits = [round(value, digits) for value in take_profits]
        return {
            "direction": direction,
            "entry": entry,
            "sl": stop,
            "risk_distance": risk,
            "atr": atr,
            "take_profits": take_profits,
            "tp_portions": self.tp_portions,
            "break_even": entry,
            "sources": {
                "swing": bool(candles),
                "order_block": bool(self._structure_levels(smc_result, direction)[:1]),
                "fair_value_gap": bool(self._structure_levels(smc_result, direction)[1:]),
                "atr": atr > 0,
            },
        }

    def trailing_stop_atr(
        self,
        position: Dict[str, Any],
        current_price: float,
        atr_value: float,
        digits: Optional[int] = None,
    ) -> Optional[float]:
        """Return a tighter dynamic ATR trailing stop, if one is available."""
        if atr_value <= 0:
            return None
        direction = "BUY" if position.get("type", BUY) == BUY else "SELL"
        current_sl = float(position.get("sl", 0.0))
        distance = float(atr_value) * self.atr_trailing_multiplier
        candidate = current_price - distance if direction == "BUY" else current_price + distance
        if direction == "BUY" and candidate <= float(position.get("price_open", 0.0)):
            return None
        if direction == "SELL" and candidate >= float(position.get("price_open", 0.0)):
            return None
        if direction == "BUY" and candidate <= current_sl:
            return None
        if direction == "SELL" and current_sl and candidate >= current_sl:
            return None
        return round(candidate, digits) if digits is not None else candidate

    def manage_position(
        self,
        position: Dict[str, Any],
        current_price: float,
        plan: Dict[str, Any],
        atr_value: Optional[float] = None,
        connector: Optional[Any] = None,
        digits: Optional[int] = None,
        completed_targets: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Calculate due actions and optionally apply them through an MT5 connector."""
        direction = plan["direction"]
        reached = []
        completed_targets = completed_targets or set()
        for index, target in enumerate(plan["take_profits"]):
            if index + 1 in completed_targets:
                continue
            if (direction == "BUY" and current_price >= target) or (direction == "SELL" and current_price <= target):
                reached.append(index + 1)
        actions: Dict[str, Any] = {"tp_reached": reached, "close_portions": [self.tp_portions[index - 1] for index in reached]}
        if 1 in reached:
            actions["break_even"] = plan["break_even"]
        trailing = self.trailing_stop_atr(position, current_price, float(atr_value or plan.get("atr", 0.0)), digits)
        if trailing is not None:
            actions["trailing_stop"] = trailing
        if connector is not None:
            ticket = position.get("ticket")
            if "trailing_stop" in actions and ticket is not None:
                connector.modify_position_sl(ticket, actions["trailing_stop"])
            elif "break_even" in actions and ticket is not None:
                connector.modify_position_sl(ticket, actions["break_even"])
            if ticket is not None and hasattr(connector, "close_partial_position"):
                for portion in actions["close_portions"]:
                    connector.close_partial_position(ticket, portion)
        return actions


SmartExit = SmartExitManager
