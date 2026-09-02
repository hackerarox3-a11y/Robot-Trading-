"""Quality gate for trading decisions."""

from typing import Any, Dict, Optional


class QualityEngine:
    """Score trade quality from 0 to 100 and reject weak setups."""

    MINIMUM_SCORE = 80.0

    def __init__(self, config: Optional[dict] = None):
        settings = (config or {}).get("quality_engine", {})
        self.minimum_score = float(settings.get("minimum_score", self.MINIMUM_SCORE))

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(100.0, float(value)))

    @staticmethod
    def _level(score: float) -> str:
        if score >= 95:
            return "Premium"
        if score >= 90:
            return "Excellent"
        if score >= 80:
            return "Moyen"
        return "Refuse"

    def _indicator_agreement(self, signal: str, scores: Dict[str, float]) -> float:
        active = [value for value in scores.values() if abs(float(value)) >= 0.05]
        if not active or signal not in ("BUY", "SELL"):
            return 0.0
        expected = 1 if signal == "BUY" else -1
        agreement = sum(1 for value in active if (1 if value > 0 else -1) == expected)
        return agreement / len(active) * 100.0

    def evaluate(self, latest: Dict[str, Any], strategy_scores: Dict[str, float],
                 signal: str, smart_money: Optional[Dict[str, Any]] = None,
                 timeframe_analysis: Optional[Dict[str, Any]] = None,
                 risk_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = risk_context or {}
        adx = float(latest.get("adx") or 0.0)
        atr = float(latest.get("atr") or 0.0)
        close = abs(float(latest.get("close") or 0.0))
        volume = float(latest.get("volume") or 0.0)
        average_volume = float(latest.get("avg_volume") or 0.0)
        rsi = float(latest.get("rsi") or 50.0)
        macd = float(latest.get("macd_line") or 0.0)
        macd_signal = float(latest.get("macd_signal") or 0.0)

        agreement = self._indicator_agreement(signal, strategy_scores)
        adx_score = 100.0 if adx >= 35 else 80.0 if adx >= 25 else 45.0 if adx >= 18 else 0.0
        atr_ratio = atr / close if close else 0.0
        atr_score = 85.0 if atr_ratio > 0 else 0.0
        if context.get("atr_ok") is False:
            atr_score = 0.0
        volume_ratio = volume / average_volume if average_volume > 0 else 0.0
        volume_score = self._clamp(volume_ratio * 60.0) if volume_ratio else 0.0

        max_spread = float(context.get("max_spread_pips") or 0.0)
        spread = context.get("spread_pips")
        if context.get("spread_ok") is False:
            spread_score = 0.0
        elif spread is None or max_spread <= 0:
            spread_score = 100.0
        else:
            spread_score = self._clamp((1.0 - float(spread) / max_spread) * 100.0)

        news_score = 0.0 if context.get("news_safe") is False else 100.0
        structure_score = 50.0
        if timeframe_analysis:
            structure_score = 100.0 if timeframe_analysis.get("h4_h1_aligned") else 0.0
        if smart_money:
            structure_score = max(structure_score, self._clamp(float(smart_money.get("confidence", 0.0)) * 100.0))

        liquidity_score = self._clamp(float(context.get("liquidity_score", 100.0)))
        momentum_score = 0.0
        if signal == "BUY":
            momentum_score = (50.0 if rsi >= 50 else 0.0) + (25.0 if macd > macd_signal else 0.0)
        elif signal == "SELL":
            momentum_score = (50.0 if rsi <= 50 else 0.0) + (25.0 if macd < macd_signal else 0.0)
        momentum_score = self._clamp(momentum_score + (25.0 if adx >= 25 else 0.0))

        components = {
            "indicator_agreement": self._clamp(agreement),
            "adx": self._clamp(adx_score),
            "atr": self._clamp(atr_score),
            "volume": self._clamp(volume_score),
            "spread": self._clamp(spread_score),
            "news": self._clamp(news_score),
            "structure": self._clamp(structure_score),
            "liquidity": self._clamp(liquidity_score),
            "momentum": self._clamp(momentum_score),
        }
        score = sum(components.values()) / len(components)
        details = ["%s: %.0f/100" % (name, value) for name, value in components.items()]
        return {
            "quality_score": round(score, 2),
            "quality_level": self._level(score),
            "accepted": score >= self.minimum_score,
            "components": components,
            "details": details,
        }
