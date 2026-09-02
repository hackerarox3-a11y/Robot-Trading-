"""
Moteur de decision multi-strategies pour le robot de trading.
Combine plusieurs strategies avec un systeme de score pondere
pour generer des signaux d'achat/vente fiables.

Strategies incluses :
  - Suivi de tendance (EMA)
  - Retournement RSI
  - Croisement MACD
  - Rebond Bollinger
  - Filtre ADX
  - Stochastique (nouveau)
  - Divergence RSI (nouveau)
  - Ichimoku Cloud (nouveau)
  - Points Pivot (nouveau)
"""

import logging
from typing import Dict, List, Optional, Tuple

from smart_money import SmartMoneyAnalyzer
from decision_engine import DecisionEngine
from quality_engine import QualityEngine

logger = logging.getLogger(__name__)


INDICATOR_WEIGHT_ALIASES = {
    "ema": ("ema", "trend_following"),
    "rsi": ("rsi", "rsi_reversal"),
    "macd": ("macd", "macd_crossover"),
    "adx": ("adx", "adx_filter"),
    "bollinger": ("bollinger", "bollinger_bounce"),
    "ichimoku": ("ichimoku",),
    "stochastic": ("stochastic",),
    "pivot": ("pivot", "pivot_points"),
    "divergence": ("divergence",),
    "volume": ("volume",),
}

DEFAULT_INDICATOR_WEIGHTS = {
    "ema": 0.20,
    "rsi": 0.10,
    "macd": 0.15,
    "adx": 0.10,
    "bollinger": 0.10,
    "ichimoku": 0.10,
    "stochastic": 0.08,
    "pivot": 0.05,
    "divergence": 0.07,
    "volume": 0.05,
}


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """Return the ten indicator weights normalized to a total of exactly 1."""
    values = {}
    for indicator, aliases in INDICATOR_WEIGHT_ALIASES.items():
        configured = next((weights.get(alias) for alias in aliases if alias in weights), None)
        try:
            value = float(configured) if configured is not None else DEFAULT_INDICATOR_WEIGHTS[indicator]
        except (TypeError, ValueError):
            value = DEFAULT_INDICATOR_WEIGHTS[indicator]
        values[indicator] = max(0.0, value)

    total = sum(values.values())
    if total <= 0:
        values = DEFAULT_INDICATOR_WEIGHTS.copy()
        total = sum(values.values())
    normalized = {key: value / total for key, value in values.items()}
    # Correct floating point residue on the final component.
    normalized["volume"] += 1.0 - sum(normalized.values())
    return normalized


class StrategyEngine:
    """
    Combine plusieurs strategies d'analyse technique avec un systeme
    de scoring pondere. Chaque strategie contribue un score entre -1 et +1.
    Le score total est compare aux seuils pour generer un signal.
    Ajoute un score de qualite (0-100) et une indication de force.
    """

    def __init__(self, config: dict, timeframe_analyzer=None):
        self.config = config
        weights = config["strategy_weights"]
        ind = config["indicators"]

        normalized_weights = normalize_weights(weights)
        self.normalized_weights = normalized_weights
        self.w_trend = normalized_weights["ema"]
        self.w_rsi = normalized_weights["rsi"]
        self.w_macd = normalized_weights["macd"]
        self.w_bollinger = normalized_weights["bollinger"]
        self.w_adx = normalized_weights["adx"]
        self.w_stochastic = normalized_weights["stochastic"]
        self.w_divergence = normalized_weights["divergence"]
        self.w_ichimoku = normalized_weights["ichimoku"]
        self.w_pivot = normalized_weights["pivot"]
        self.w_volume = normalized_weights["volume"]
        self.w_smart_money = float(config.get("smart_money", {}).get("weight", 0.15))
        self.smart_money = SmartMoneyAnalyzer(config)
        self.decision_engine = DecisionEngine(config)
        self.quality_engine = QualityEngine(config)
        self.timeframe_analyzer = timeframe_analyzer

        # Seuils de base
        self.buy_threshold = weights["buy_threshold"]
        self.sell_threshold = weights["sell_threshold"]

        # Parametres indicateurs
        self.rsi_overbought = ind["rsi_overbought"]
        self.rsi_oversold = ind["rsi_oversold"]
        self.adx_threshold = ind["adx_threshold"]

    # ------------------------------------------------------------------
    #  STRATEGIES INDIVIDUELLES EXISTANTES
    # ------------------------------------------------------------------

    def _score_trend_following(self, vals: Dict) -> float:
        """
        Strategie de suivi de tendance.
        - EMA fast > EMA medium > EMA slow = tendance haussiere (+1)
        - EMA fast < EMA medium < EMA slow = tendance baissiere (-1)
        - Position par rapport a EMA 200 (trend long terme)
        """
        score = 0.0
        ema_f = vals.get("ema_fast")
        ema_m = vals.get("ema_medium")
        ema_s = vals.get("ema_slow")
        ema_t = vals.get("ema_trend")
        close = vals.get("close")

        if any(v is None for v in [ema_f, ema_m, ema_s]):
            return 0.0

        if ema_f > ema_m > ema_s:
            score += 0.6
        elif ema_f < ema_m < ema_s:
            score -= 0.6
        elif ema_f > ema_m:
            score += 0.3
        elif ema_f < ema_m:
            score -= 0.3

        if ema_t is not None and close is not None:
            if close > ema_t:
                score += 0.4
            else:
                score -= 0.4

        return max(-1.0, min(1.0, score))

    def _score_rsi_reversal(self, vals: Dict) -> float:
        """
        Strategie de retournement RSI.
        - RSI < oversold = signal d'achat
        - RSI > overbought = signal de vente
        - Zone neutre = pas de signal
        """
        rsi = vals.get("rsi")
        if rsi is None:
            return 0.0

        if rsi <= self.rsi_oversold:
            return 0.8
        elif rsi >= self.rsi_overbought:
            return -0.8
        elif rsi <= 40:
            return 0.4
        elif rsi >= 60:
            return -0.4
        else:
            return 0.0

    def _score_macd_crossover(self, vals: Dict) -> float:
        """
        Strategie de croisement MACD.
        - MACD > Signal et histogramme croissant = achat
        - MACD < Signal et histogramme decroissant = vente
        """
        macd_line = vals.get("macd_line")
        signal_line = vals.get("macd_signal")
        macd_hist = vals.get("macd_hist")

        if any(v is None for v in [macd_line, signal_line, macd_hist]):
            return 0.0

        score = 0.0

        if macd_line > signal_line:
            score += 0.5
        elif macd_line < signal_line:
            score -= 0.5

        if macd_hist > 0:
            score += 0.3
        elif macd_hist < 0:
            score -= 0.3

        if macd_line > 0 and signal_line > 0:
            score += 0.2
        elif macd_line < 0 and signal_line < 0:
            score -= 0.2

        return max(-1.0, min(1.0, score))

    def _score_bollinger_bounce(self, vals: Dict) -> float:
        """
        Strategie de rebond sur Bandes de Bollinger.
        - Prix pres de la bande inferieure = signal d'achat
        - Prix pres de la bande superieure = signal de vente
        """
        bb_upper = vals.get("bb_upper")
        bb_lower = vals.get("bb_lower")
        bb_middle = vals.get("bb_middle")
        close = vals.get("close")

        if any(v is None for v in [bb_upper, bb_lower, bb_middle, close]):
            return 0.0

        bb_range = bb_upper - bb_lower
        if bb_range == 0:
            return 0.0

        bb_position = (close - bb_lower) / bb_range

        if bb_position <= 0.05:
            return 0.9
        elif bb_position <= 0.15:
            return 0.6
        elif bb_position >= 0.95:
            return -0.9
        elif bb_position >= 0.85:
            return -0.6
        elif close > bb_upper:
            return -0.7
        elif close < bb_lower:
            return 0.7
        else:
            return 0.0

    def _score_adx_filter(self, vals: Dict) -> float:
        """
        Filtre ADX pour confirmer la force de la tendance.
        - ADX > seuil = tendance forte, le signal est renforce
        - ADX < seuil = tendance faible, le signal est attenue
        """
        adx = vals.get("adx")
        plus_di = vals.get("plus_di")
        minus_di = vals.get("minus_di")

        if adx is None:
            return 0.0

        if adx < self.adx_threshold:
            return 0.0

        if plus_di is not None and minus_di is not None:
            if plus_di > minus_di:
                return 0.6
            else:
                return -0.6

        return 0.3

    def _score_volume(self, vals: Dict) -> float:
        """Use volume as directional confirmation, never as a standalone signal."""
        volume = vals.get("volume")
        average = vals.get("avg_volume")
        if volume is None or average is None or float(average) <= 0:
            return 0.0
        ratio = float(volume) / float(average)
        if ratio < 1.0:
            return 0.0
        ema_fast = vals.get("ema_fast")
        ema_slow = vals.get("ema_slow")
        if ema_fast is None or ema_slow is None or ema_fast == ema_slow:
            return 0.0
        strength = min(1.0, (ratio - 1.0) / 1.5)
        return strength if ema_fast > ema_slow else -strength

    # ------------------------------------------------------------------
    #  NOUVELLES STRATEGIES
    # ------------------------------------------------------------------

    def _score_stochastic(self, vals: Dict) -> float:
        """
        Strategie Stochastique.
        Croisement %K/%D dans les zones de survente/surachat.
        - %K coupe %D a la hausse en zone survente (<20) = achat
        - %K coupe %D a la baisse en zone surachat (>80) = vente
        """
        stoch_k = vals.get("stoch_k")
        stoch_d = vals.get("stoch_d")
        stoch_k_prev = vals.get("stoch_k_prev")
        stoch_d_prev = vals.get("stoch_d_prev")

        if stoch_k is None or stoch_d is None:
            return 0.0

        score = 0.0

        # Zones extremes
        if stoch_k <= 20 and stoch_d <= 20:
            score += 0.3  # Zone de survente
        elif stoch_k >= 80 and stoch_d >= 80:
            score -= 0.3  # Zone de surachat

        # Croisement K/D (si donnees precedentes disponibles)
        if (stoch_k_prev is not None and stoch_d_prev is not None
                and stoch_k_prev is not None and stoch_d_prev is not None):
            # Croisement haussier : K passe au-dessus de D
            if stoch_k_prev <= stoch_d_prev and stoch_k > stoch_d:
                if stoch_k < 30:
                    score += 0.6  # Croisement en zone survente = fort signal
                elif stoch_k < 50:
                    score += 0.3
            # Croisement baissier : K passe en-dessous de D
            elif stoch_k_prev >= stoch_d_prev and stoch_k < stoch_d:
                if stoch_k > 70:
                    score -= 0.6  # Croisement en zone surachat = fort signal
                elif stoch_k > 50:
                    score -= 0.3
        else:
            # Sans donnees prev, utiliser la position relative
            if stoch_k > stoch_d and stoch_k < 40:
                score += 0.3
            elif stoch_k < stoch_d and stoch_k > 60:
                score -= 0.3

        return max(-1.0, min(1.0, score))

    def _score_divergence(self, vals: Dict) -> float:
        """
        Strategie de divergence RSI / Prix.
        - Divergence haussiere = signal d'achat
        - Divergence baissiere = signal de vente
        """
        divergence = vals.get("divergence")

        if divergence is None or divergence == "none":
            return 0.0

        if divergence == "bullish":
            # Confirmer avec RSI pas trop eleve
            rsi = vals.get("rsi")
            if rsi is not None and rsi < 60:
                return 0.9
            return 0.5
        elif divergence == "bearish":
            rsi = vals.get("rsi")
            if rsi is not None and rsi > 40:
                return -0.9
            return -0.5

        return 0.0

    def _score_ichimoku(self, vals: Dict) -> float:
        """
        Strategie Ichimoku Cloud.
        - Prix au-dessus du nuage = tendance haussiere
        - Prix en-dessous du nuage = tendance baissiere
        - Croisement Tenkan/Kijun = changement de tendance
        """
        tenkan = vals.get("ichimoku_tenkan")
        kijun = vals.get("ichimoku_kijun")
        senkou_a = vals.get("ichimoku_senkou_a")
        senkou_b = vals.get("ichimoku_senkou_b")
        close = vals.get("close")

        if any(v is None for v in [tenkan, kijun, senkou_a, senkou_b, close]):
            return 0.0

        score = 0.0

        # Position du prix par rapport au nuage (Senkou A/B)
        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)

        if cloud_top - cloud_bottom > 1e-10:
            if close > cloud_top:
                score += 0.4  # Au-dessus du nuage = haussier
            elif close < cloud_bottom:
                score -= 0.4  # En-dessous du nuage = baissier
            else:
                score += 0.0  # Dans le nuage = neutre

        # Couleur du nuage (vert = haussier, rouge = baissier)
        if senkou_a > senkou_b:
            score += 0.1
        else:
            score -= 0.1

        # Croisement Tenkan/Kijun (changement de momentum)
        tenkan_prev = vals.get("ichimoku_tenkan_prev")
        kijun_prev = vals.get("ichimoku_kijun_prev")
        if (tenkan_prev is not None and kijun_prev is not None):
            if tenkan_prev <= kijun_prev and tenkan > kijun:
                score += 0.4  # Croisement haussier TK
                logger.debug("Croisement Tenkan/Kijun haussier detecte")
            elif tenkan_prev >= kijun_prev and tenkan < kijun:
                score -= 0.4  # Croisement baissier TK
                logger.debug("Croisement Tenkan/Kijun baissier detecte")
        else:
            # Sans prev, position relative
            if tenkan > kijun:
                score += 0.2
            else:
                score -= 0.2

        return max(-1.0, min(1.0, score))

    def _score_pivot_points(self, vals: Dict) -> float:
        """
        Strategie Points Pivot.
        Rebond sur les niveaux de support/resistance.
        - Prix proche d'un support = signal d'achat
        - Prix proche d'une resistance = signal de vente
        """
        pivots = vals.get("pivots_daily")
        close = vals.get("close")

        if pivots is None or not pivots or close is None:
            return 0.0

        score = 0.0
        pp = pivots.get("pp")
        r1 = pivots.get("r1")
        s1 = pivots.get("s1")
        r2 = pivots.get("r2")
        s2 = pivots.get("s2")

        if pp is None or close == 0:
            return 0.0

        # Distance en pourcentage par rapport au PP
        pp_dist_pct = abs(close - pp) / pp * 100.0

        # Prix au-dessus du pivot point = biais haussier
        if close > pp:
            score += 0.2
            # Proximite d'une resistance
            if r1 is not None and r2 is not None:
                r1_dist = abs(close - r1) / close * 100.0
                r2_dist = abs(close - r2) / close * 100.0
                if r1_dist < 0.05:
                    score -= 0.5  # Tres proche de R1 = signal de vente
                elif r1_dist < 0.15:
                    score -= 0.3
                if r2_dist < 0.05:
                    score -= 0.7  # Tres proche de R2 = fort signal de vente
        else:
            score -= 0.2
            # Proximite d'un support
            if s1 is not None and s2 is not None:
                s1_dist = abs(close - s1) / close * 100.0
                s2_dist = abs(close - s2) / close * 100.0
                if s1_dist < 0.05:
                    score += 0.5  # Tres proche de S1 = signal d'achat
                elif s1_dist < 0.15:
                    score += 0.3
                if s2_dist < 0.05:
                    score += 0.7  # Tres proche de S2 = fort signal d'achat

        return max(-1.0, min(1.0, score))

    # ------------------------------------------------------------------
    #  SEUILS ADAPTATIFS ET QUALITE
    # ------------------------------------------------------------------

    def _get_adaptive_thresholds(self, vals: Dict) -> Tuple[float, float]:
        """
        Ajuste les seuils d'achat/vente en fonction du regime de volatilite.
        En volatilite elevee, les seuils sont serres pour eviter les faux signaux.
        En volatilite faible, les seuils sont elargis.
        """
        regime = vals.get("volatility_regime", "medium")

        if regime == "extreme":
            # Seuils plus serres en volatilite extreme
            factor_buy = 1.3
            factor_sell = 1.3
        elif regime == "high":
            factor_buy = 1.15
            factor_sell = 1.15
        elif regime == "low":
            # Seuils plus larges en volatilite faible
            factor_buy = 0.85
            factor_sell = 0.85
        else:
            factor_buy = 1.0
            factor_sell = 1.0

        adapted_buy = self.buy_threshold * factor_buy
        adapted_sell = self.sell_threshold * factor_sell

        return adapted_buy, adapted_sell

    def _calculate_volume_confirmation(self, vals: Dict) -> float:
        """
        Confirme le signal par le volume si disponible.
        Compare le volume actuel a la moyenne mobile du volume.

        Returns:
            0.0 si pas de volume, sinon entre 0.0 et 1.0
        """
        volume = vals.get("volume")
        avg_volume = vals.get("avg_volume")

        if volume is None or avg_volume is None or avg_volume <= 0:
            return 0.0

        vol_ratio = volume / avg_volume
        if vol_ratio >= 2.0:
            return 1.0
        elif vol_ratio >= 1.5:
            return 0.8
        elif vol_ratio >= 1.2:
            return 0.5
        elif vol_ratio >= 0.8:
            return 0.3
        else:
            return 0.1

    def _calculate_signal_quality(self, scores: Dict[str, float],
                                   total: float, vals: Dict) -> int:
        """
        Calcule un score de qualite global (0-100) combinant
        plusieurs facteurs de confirmation.

        Facteurs :
        - Nombre de strategies d'accord (+20 max)
        - Force du score total (+25 max)
        - Confirmation volume (+15 max)
        - Direction ADX (+15 max)
        - Regime de volatilite (+10 max)
        - Absence de signaux contradictoires (+15 max)
        """
        quality = 0

        # 1. Nombre de strategies en accord avec le signal (+20)
        direction = 1 if total > 0 else (-1 if total < 0 else 0)
        if direction != 0:
            agreeing = sum(1 for s in scores.values()
                         if (direction > 0 and s > 0.1) or (direction < 0 and s < -0.1))
            total_strategies = len(scores)
            agreement_ratio = agreeing / total_strategies if total_strategies > 0 else 0
            quality += int(agreement_ratio * 20)

        # 2. Force du score total (+25)
        abs_score = abs(total)
        if abs_score >= 0.8:
            quality += 25
        elif abs_score >= 0.6:
            quality += 20
        elif abs_score >= 0.4:
            quality += 15
        elif abs_score >= 0.2:
            quality += 10
        else:
            quality += 3

        # 3. Confirmation volume (+15)
        vol_conf = self._calculate_volume_confirmation(vals)
        quality += int(vol_conf * 15)

        # 4. Direction ADX (+15)
        adx = vals.get("adx")
        plus_di = vals.get("plus_di")
        minus_di = vals.get("minus_di")
        if adx is not None and adx >= 25:
            if direction > 0 and plus_di is not None and minus_di is not None:
                if plus_di > minus_di:
                    quality += 15
                else:
                    quality += 5
            elif direction < 0 and plus_di is not None and minus_di is not None:
                if minus_di > plus_di:
                    quality += 15
                else:
                    quality += 5
            else:
                quality += 8

        # 5. Regime de volatilite (+10)
        regime = vals.get("volatility_regime", "medium")
        if regime == "medium":
            quality += 10
        elif regime == "low":
            quality += 7
        elif regime == "high":
            quality += 4
        elif regime == "extreme":
            quality += 1

        # 6. Absence de signaux contradictoires forts (+15)
        if direction != 0:
            opposing = sum(1 for s in scores.values()
                          if (direction > 0 and s < -0.3) or (direction < 0 and s > 0.3))
            if opposing == 0:
                quality += 15
            elif opposing == 1:
                quality += 8
            elif opposing >= 3:
                quality += 0
            else:
                quality += 3

        return min(100, max(0, quality))

    def _determine_signal_strength(self, scores: Dict[str, float],
                                    total: float, quality: int) -> str:
        """
        Determine la force du signal : faible, moyen ou fort.

        Criteria :
        - Fort : qualite >= 70, score absolu >= 0.5, peu de conflits
        - Moyen : qualite >= 45, score absolu >= 0.3
        - Faible : tout le reste
        """
        abs_total = abs(total)
        direction = 1 if total > 0 else (-1 if total < 0 else 0)

        if direction == 0:
            return "faible"

        # Compter les conflits
        opposing = sum(1 for s in scores.values()
                      if (direction > 0 and s < -0.3) or (direction < 0 and s > 0.3))

        if quality >= 70 and abs_total >= 0.5 and opposing <= 1:
            return "fort"
        elif quality >= 45 and abs_total >= 0.3:
            return "moyen"
        else:
            return "faible"

    # ------------------------------------------------------------------
    #  COMBINAISON DES SCORES
    # ------------------------------------------------------------------

    def generate_signal(self, latest_values: Dict, close_price: float,
                        ohlc_data: Optional[Dict] = None,
                        symbol: Optional[str] = None) -> Dict:
        """
        Combine toutes les strategies et genere un signal de trading.

        Args:
            latest_values: Dictionnaire des dernieres valeurs d'indicateurs
            close_price: Prix de cloture actuel

        Returns:
            {
                'signal': 'BUY' | 'SELL' | 'HOLD',
                'total_score': float,
                'strategy_scores': dict,
                'confidence': float (0-100%),
                'quality_score': int (0-100),
                'strength': 'faible' | 'moyen' | 'fort',
            }
        """
        vals = dict(latest_values)
        vals["close"] = close_price

        # Seuils adaptatifs
        adapted_buy, adapted_sell = self._get_adaptive_thresholds(vals)

        # Calculer le score de chaque strategie
        scores = {
            "trend_following": self._score_trend_following(vals),
            "rsi_reversal": self._score_rsi_reversal(vals),
            "macd_crossover": self._score_macd_crossover(vals),
            "bollinger_bounce": self._score_bollinger_bounce(vals),
            "adx_filter": self._score_adx_filter(vals),
            "stochastic": self._score_stochastic(vals),
            "divergence": self._score_divergence(vals),
            "ichimoku": self._score_ichimoku(vals),
            "pivot_points": self._score_pivot_points(vals),
            "volume": self._score_volume(vals),
        }

        # Score total pondere
        total = (
            self.w_trend * scores["trend_following"]
            + self.w_rsi * scores["rsi_reversal"]
            + self.w_macd * scores["macd_crossover"]
            + self.w_bollinger * scores["bollinger_bounce"]
            + self.w_adx * scores["adx_filter"]
            + self.w_stochastic * scores["stochastic"]
            + self.w_divergence * scores["divergence"]
            + self.w_ichimoku * scores["ichimoku"]
            + self.w_pivot * scores["pivot_points"]
            + self.w_volume * scores["volume"]
        )

        smart_money = self.smart_money.analyze(ohlc_data) if ohlc_data is not None else {
            "trend_structure": "ranging", "confidence": 0.0
        }
        smart_money_score = self.smart_money.score(smart_money) if ohlc_data is not None else 0.0
        total += self.w_smart_money * smart_money_score

        # Determiner le signal avec seuils adaptatifs, apres confirmation SMC.
        if total >= adapted_buy:
            signal = "BUY"
        elif total <= adapted_sell:
            signal = "SELL"
        else:
            signal = "HOLD"

        # Confiance (basee sur les seuils adaptatifs)
        if signal == "BUY":
            denom = max(1.0 - adapted_buy, 1e-10)
            confidence = min(100, ((total - adapted_buy) / denom) * 100)
        elif signal == "SELL":
            denom = max(1.0 - abs(adapted_sell), 1e-10)
            confidence = min(100, ((abs(total) - abs(adapted_sell)) / denom) * 100)
        else:
            max_thresh = max(abs(adapted_buy), abs(adapted_sell))
            confidence = max(0, (1.0 - abs(total) / max_thresh) * 100)

        # Une structure SMC forte peut invalider un signal technique oppose.
        if smart_money.get("confidence", 0.0) >= self.smart_money.min_confidence:
            smc_direction = "BUY" if smart_money_score > 0.35 else "SELL" if smart_money_score < -0.35 else None
            if smc_direction and signal not in ("HOLD", smc_direction):
                signal = "HOLD"
                confidence = min(confidence, 50.0)

        timeframe_result = None
        if self.timeframe_analyzer is not None and symbol:
            timeframe_result = self.timeframe_analyzer.combine_timeframes(symbol, signal)
            if not timeframe_result.get("allowed", False):
                signal = "HOLD"

        decision = self.decision_engine.evaluate(
            latest=vals,
            technical_signal=signal,
            smart_money=smart_money,
            timeframe_analysis=timeframe_result,
        )
        signal = decision["signal"]
        quality = self.quality_engine.evaluate(
            latest=vals,
            strategy_scores=scores,
            signal=signal,
            smart_money=smart_money,
            timeframe_analysis=timeframe_result,
        )
        if not quality["accepted"]:
            signal = "HOLD"

        # Score de qualite
        quality = self._calculate_signal_quality(scores, total, vals)

        # Force du signal
        strength = self._determine_signal_strength(scores, total, quality)

        result = {
            "signal": signal,
            "total_score": round(total, 4),
            "strategy_scores": {k: round(v, 4) for k, v in scores.items()},
            "confidence": round(max(0, confidence), 1),
            "quality_score": quality,
            "strength": strength,
            "smart_money": smart_money,
            "smart_money_score": round(smart_money_score, 4),
            "timeframe_analysis": timeframe_result,
            "decision_score": decision["decision_score"],
            "buy_probability": decision["buy_probability"],
            "sell_probability": decision["sell_probability"],
            "decision_confidence": decision["confidence"],
            "reasons": decision["reasons"],
            "quality_score": quality["quality_score"],
            "quality_level": quality["quality_level"],
            "quality_accepted": quality["accepted"],
            "quality_details": quality["details"],
        }

        logger.info(
            f"Signal genere : {signal} | Score={total:.4f} | "
            f"Confiance={confidence:.1f}% | Qualite={quality}/100 | {strength}"
        )
        return result
