"""Decision Engine V5: structure, momentum and risk layers."""

from typing import Any, Dict, List, Optional


class DecisionEngine:
    """Convert technical, SMC and multi-timeframe evidence into probabilities."""

    MIN_PROBABILITY = 92.0

    def __init__(self, config: Optional[dict] = None):
        settings = (config or {}).get("decision_engine", {})
        self.min_probability = float(settings.get("min_probability", self.MIN_PROBABILITY))
        self.structure_weight = float(settings.get("structure_weight", 0.40))
        self.momentum_weight = float(settings.get("momentum_weight", 0.35))
        self.risk_weight = float(settings.get("risk_weight", 0.25))

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
        return max(low, min(high, float(value)))

    @staticmethod
    def _direction_score(direction: str, bullish: float, bearish: float) -> float:
        if direction == "bullish":
            return bullish
        if direction == "bearish":
            return bearish
        return max(bullish, bearish) * 0.5

    def _structure_layer(self, smc: Dict[str, Any], mtf: Optional[Dict[str, Any]]) -> Dict[str, float]:
        bullish = bearish = 0.0
        structure = smc.get("trend_structure", "ranging") if smc else "ranging"
        if structure == "bullish":
            bullish += 35.0
        elif structure == "bearish":
            bearish += 35.0

        for key in ("bos", "choch", "liquidity_sweep"):
            direction = (smc.get(key, {}) if smc else {}).get("direction")
            if direction == "bullish":
                bullish += 20.0 if key != "liquidity_sweep" else 12.0
            elif direction == "bearish":
                bearish += 20.0 if key != "liquidity_sweep" else 12.0

        if smc and smc.get("bullish_order_block", {}).get("detected"):
            bullish += 8.0
        if smc and smc.get("bearish_order_block", {}).get("detected"):
            bearish += 8.0
        if smc and smc.get("bullish_fvg", {}).get("detected"):
            bullish += 7.0
        if smc and smc.get("bearish_fvg", {}).get("detected"):
            bearish += 7.0

        if mtf:
            rules = mtf.get("rules", {})
            if mtf.get("h4_h1_aligned"):
                if rules.get("h4") == "bullish":
                    bullish += 20.0
                elif rules.get("h4") == "bearish":
                    bearish += 20.0
            else:
                bullish -= 25.0
                bearish -= 25.0

        return {"bullish": self._clamp(bullish), "bearish": self._clamp(bearish)}

    def _momentum_layer(self, latest: Dict[str, Any]) -> Dict[str, float]:
        bullish = bearish = 0.0
        rsi = latest.get("rsi")
        if rsi is not None:
            if rsi >= 55:
                bullish += min(25.0, (float(rsi) - 50.0) * 0.8)
            elif rsi <= 45:
                bearish += min(25.0, (50.0 - float(rsi)) * 0.8)

        macd = latest.get("macd_line")
        macd_signal = latest.get("macd_signal")
        if macd is not None and macd_signal is not None:
            if macd > macd_signal:
                bullish += 25.0
            elif macd < macd_signal:
                bearish += 25.0

        adx = latest.get("adx")
        plus_di = latest.get("plus_di")
        minus_di = latest.get("minus_di")
        if adx is not None and float(adx) >= 20 and plus_di is not None and minus_di is not None:
            if plus_di > minus_di:
                bullish += 25.0
            elif minus_di > plus_di:
                bearish += 25.0

        return {"bullish": self._clamp(bullish), "bearish": self._clamp(bearish)}

    def _risk_layer(self, risk_context: Optional[Dict[str, Any]], mtf: Optional[Dict[str, Any]]) -> float:
        context = risk_context or {}
        score = 100.0
        if context.get("news_safe") is False:
            score = 0.0
        if context.get("spread_ok") is False:
            score = 0.0
        if context.get("lot_feasible") is False:
            score = 0.0
        if context.get("risk_allowed") is False:
            score = 0.0
        if mtf and not mtf.get("h4_h1_aligned", False):
            score = 0.0
        return self._clamp(score)

    def evaluate(self, latest: Dict[str, Any], technical_signal: str = "HOLD",
                 smart_money: Optional[Dict[str, Any]] = None,
                 timeframe_analysis: Optional[Dict[str, Any]] = None,
                 risk_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        structure = self._structure_layer(smart_money or {}, timeframe_analysis)
        momentum = self._momentum_layer(latest)
        risk = self._risk_layer(risk_context, timeframe_analysis)

        structure_score = max(structure.values())
        momentum_score = max(momentum.values())
        decision_score = (
            self.structure_weight * structure_score
            + self.momentum_weight * momentum_score
            + self.risk_weight * risk
        )

        directional_edge = (
            self.structure_weight * (structure["bullish"] - structure["bearish"]) +
            self.momentum_weight * (momentum["bullish"] - momentum["bearish"])
        ) / max(self.structure_weight + self.momentum_weight, 1e-9)
        risk_factor = risk / 100.0
        bullish_probability = self._clamp(50.0 + 0.5 * directional_edge * risk_factor)
        bearish_probability = self._clamp(50.0 - 0.5 * directional_edge * risk_factor)

        signal = "BUY" if bullish_probability >= self.min_probability else "SELL" if bearish_probability >= self.min_probability else "HOLD"
        if technical_signal in ("BUY", "SELL") and signal != technical_signal:
            signal = "HOLD"

        confidence = self._clamp(max(bullish_probability, bearish_probability) * (0.5 + risk_factor / 2.0))
        reasons: List[str] = [
            "Structure: %.1f/100 (bullish %.1f, bearish %.1f)" % (structure_score, structure["bullish"], structure["bearish"]),
            "Momentum: %.1f/100 (bullish %.1f, bearish %.1f)" % (momentum_score, momentum["bullish"], momentum["bearish"]),
            "Filtres de risque: %.1f/100" % risk,
        ]
        if timeframe_analysis and not timeframe_analysis.get("h4_h1_aligned", False):
            reasons.append("Trade bloque: H4 et H1 ne sont pas alignes")
        if signal == "HOLD":
            reasons.append("Aucune probabilite BUY/SELL n'atteint 92%")
        else:
            reasons.append("Seuil de probabilite 92% atteint")

        return {
            "signal": signal,
            "decision_score": round(decision_score, 2),
            "buy_probability": round(bullish_probability, 2),
            "sell_probability": round(bearish_probability, 2),
            "confidence": round(confidence, 2),
            "reasons": reasons,
            "layers": {"structure": structure_score, "momentum": momentum_score, "risk": risk},
        }
