"""
Sélecteur intelligent de marché.
Analyse tous les marchés disponibles et sélectionne le plus favorable
pour maximiser les gains avec un petit capital (5$).

Critères d'évaluation :
  - Force de tendance (ADX)
  - Volatilité optimale (ATR)
  - Momentum (RSI)
  - Alignement des moyennes mobiles
  - Score de confiance du signal
  - Filtre de corrélation (nouveau)
  - Estimation spread/liquidité (nouveau)
  - Score regime de volatilité (nouveau)
  - Score de session (nouveau)
  - Performance récente (nouveau)
  - Accélération du momentum (nouveau)
"""

import logging
import numpy as np
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# Heures UTC approximatives des sessions de trading
SESSION_WINDOWS = {
    "sydney": (21, 6),      # 21h - 6h UTC
    "tokyo": (0, 9),        # 0h - 9h UTC
    "london": (7, 16),      # 7h - 16h UTC
    "new_york": (12, 21),   # 12h - 21h UTC
}


# Paires de devises corrélées (matrice de corrélation simplifiee)
CORRELATED_PAIRS: Dict[str, List[str]] = {
    "EURUSD": ["GBPUSD", "AUDUSD", "NZDUSD", "USDCHF"],
    "GBPUSD": ["EURUSD", "AUDUSD", "NZDUSD"],
    "USDJPY": ["EURJPY", "GBPJPY", "AUDJPY"],
    "AUDUSD": ["EURUSD", "GBPUSD", "NZDUSD", "AUDJPY"],
    "USDCAD": ["AUDCAD", "USDCAD"],
    "USDCHF": ["EURUSD", "GBPUSD"],
}

# Spread moyen estimé par type de symbole (en pips)
ESTIMATED_SPREADS: Dict[str, float] = {
    "major": 1.5,
    "minor": 3.0,
    "exotic": 10.0,
    "synthetic": 0.5,
    "metal": 3.0,
    "index": 5.0,
}


class MarketSelector:
    """
    Sélectionne automatiquement le meilleur marché à trader.
    Fonctionne avec un petit capital en cherchant le meilleur
    rapport risque/récompense.
    """

    def __init__(self, config: dict):
        self.config = config
        self.scan_history: Dict[str, List[float]] = {}
        self.max_history = 20
        self.active_symbols: Set[str] = set()
        self.recent_performance: Dict[str, List[float]] = {}
        self.max_performance_history = 10
        self.correlation_threshold = config.get("correlation_threshold", 0.7)

    def evaluate_market(self, symbol: str, analysis: Dict,
                          latest_values: Dict, current_price: float) -> Dict:
        """
        Évalue un marché et retourne un score de favorabilité.

        Score global : 0 à 100
        - > 70 : Excellent, trader maintenant
        - 50-70 : Bon, surveiller
        - < 50 : Éviter

        Returns:
            {score, breakdown, recommendation}
        """
        breakdown: Dict = {}
        total = 0.0

        # 1. Force de tendance (0-20 points)
        adx_score = self._score_trend_strength(latest_values, breakdown)
        total += adx_score

        # 2. Qualité du signal (0-20 points)
        signal_score = self._score_signal_quality(latest_values, current_price, breakdown)
        total += signal_score

        # 3. Volatilité (0-15 points)
        vol_score = self._score_volatility(latest_values, breakdown)
        total += vol_score

        # 4. Momentum (0-12 points)
        mom_score = self._score_momentum(latest_values, breakdown)
        total += mom_score

        # 5. Régularité historique (0-10 points)
        reg_score = self._score_consistency(symbol, breakdown)
        total += reg_score

        # 6. Score de session (0-8 points, nouveau)
        session_score = self._score_session(breakdown)
        total += session_score

        # 7. Score de corrélation (0-8 points, nouveau)
        corr_score = self._score_correlation(symbol, breakdown)
        total += corr_score

        # 8. Score de spread/liquidité (0-7 points, nouveau)
        spread_score = self._score_spread_liquidity(symbol, latest_values, current_price, breakdown)
        total += spread_score

        score = round(min(100, total), 1)

        if score >= 70:
            recommendation = "EXCELLENT"
        elif score >= 50:
            recommendation = "BON"
        else:
            recommendation = "EVITER"

        self._save_score(symbol, score)

        return {
            "score": score,
            "recommendation": recommendation,
            "breakdown": breakdown,
        }

    def select_best_market(self, evaluations: Dict[str, Dict]) -> Optional[str]:
        """
        Sélectionne le meilleur marché parmi tous ceux évalués.
        Ne retourne que les marchés avec un score >= 50.
        """
        best_symbol = None
        best_score = 0

        for symbol, ev in evaluations.items():
            score = ev["score"]
            rec = ev["recommendation"]

            if rec == "EVITER":
                continue

            if score > best_score:
                best_score = score
                best_symbol = symbol

        if best_symbol:
            self.active_symbols.add(best_symbol)
            logger.info(f"Marché sélectionné : {best_symbol} (score={best_score})")
        else:
            logger.debug("Aucun marché favorable trouvé ce cycle.")

        return best_symbol

    def record_trade_result(self, symbol: str, pnl_pct: float) -> None:
        """
        Enregistre le résultat d'un trade pour le suivi de performance.
        Utilisé pour éviter les symboles avec de récentes pertes importantes.

        Args:
            symbol: Symbole tradé
            pnl_pct: Pourcentage de P&L (positif = gain, négatif = perte)
        """
        if symbol not in self.recent_performance:
            self.recent_performance[symbol] = []
        self.recent_performance[symbol].append(pnl_pct)
        if len(self.recent_performance[symbol]) > self.max_performance_history:
            self.recent_performance[symbol] = self.recent_performance[symbol][-self.max_performance_history:]
        logger.debug(f"Résultat enregistré pour {symbol}: {pnl_pct:+.2f}%")

    def update_active_symbols(self, symbols: Set[str]) -> None:
        """
        Met à jour l'ensemble des symboles actuellement actifs.
        Utilisé pour le filtre de corrélation.
        """
        self.active_symbols = set(symbols)

    # ------------------------------------------------------------------
    #  SCORES EXISTANTS (modifiés)
    # ------------------------------------------------------------------

    def _score_trend_strength(self, vals: Dict, bd: Dict) -> float:
        """Évalue la force de la tendance via ADX et alignement EMA. (0-20)"""
        score = 0.0
        adx = vals.get("adx")
        plus_di = vals.get("plus_di")
        minus_di = vals.get("minus_di")

        if adx is not None:
            if adx >= 40:
                score += 8
            elif adx >= 30:
                score += 6
            elif adx >= 20:
                score += 3

            if plus_di is not None and minus_di is not None:
                di_diff = abs(plus_di - minus_di)
                if di_diff >= 20:
                    score += 6
                elif di_diff >= 10:
                    score += 3
                else:
                    score += 1

            if plus_di > minus_di and plus_di > 20:
                bd["trend_dir"] = "HAUSSIER"
                score += 6
            elif minus_di > plus_di and minus_di > 20:
                bd["trend_dir"] = "BAISSIER"
                score += 6
            else:
                bd["trend_dir"] = "NEUTRE"

        bd["adx"] = round(adx, 1) if adx else None
        return score

    def _score_signal_quality(self, vals: Dict, price: float, bd: Dict) -> float:
        """Évalue la qualité du signal via EMA, MACD et indicateurs avancés. (0-20)"""
        score = 0.0
        ema_f = vals.get("ema_fast")
        ema_m = vals.get("ema_medium")
        ema_s = vals.get("ema_slow")
        macd_hist = vals.get("macd_hist")
        close = vals.get("close", price)

        # Alignement EMA (7 points max)
        if all(v is not None for v in [ema_f, ema_m, ema_s]):
            if ema_f > ema_m > ema_s:
                score += 7
                bd["ema_align"] = "HAUSSIER"
            elif ema_f < ema_m < ema_s:
                score += 7
                bd["ema_align"] = "BAISSIER"
            elif ema_f > ema_m:
                score += 3
                bd["ema_align"] = "MIXTE_H"
            elif ema_f < ema_m:
                score += 3
                bd["ema_align"] = "MIXTE_B"
            else:
                bd["ema_align"] = "NEUTRE"

        # MACD histogramme directionnel (6 points max)
        if macd_hist is not None:
            if abs(macd_hist) > 0:
                score += 3
                if macd_hist > 0:
                    bd["macd_dir"] = "HAUSSIER"
                else:
                    bd["macd_dir"] = "BAISSIER"

                macd_line = vals.get("macd_line", 0) or 0
                if abs(macd_hist) > abs(macd_line) * 0.5:
                    score += 3

        # Prix au-dessus/au-dessous des EMA (7 points)
        if all(v is not None for v in [ema_f, ema_m, ema_s, close]):
            if close > ema_f > ema_m:
                score += 7
            elif close < ema_f < ema_m:
                score += 7
            elif close > ema_f:
                score += 2

        return score

    def _score_volatility(self, vals: Dict, bd: Dict) -> float:
        """Évalue si la volatilité est bonne pour trader. (0-15)
        Inclut le regime de volatilité pour un scoring plus precis.
        """
        score = 0.0
        atr = vals.get("atr")
        close = vals.get("close")
        vol_regime = vals.get("volatility_regime", "unknown")

        if atr is None or close is None or close == 0:
            bd["volatilité"] = "N/A"
            return 0

        atr_pct = (atr / close) * 100
        bd["atr_pct"] = round(atr_pct, 4)
        bd["regime_volatilité"] = vol_regime

        # Scoring base sur l'ATR en pourcentage
        if 0.05 <= atr_pct <= 0.5:
            score += 10
        elif 0.01 <= atr_pct < 0.05:
            score += 5
        elif 0.5 < atr_pct <= 2.0:
            score += 7
        elif atr_pct > 2.0:
            score += 2
        else:
            score += 1

        # Bonus regime de volatilité (5 points)
        if vol_regime == "medium":
            score += 5
            bd["regime_score"] = "optimal"
        elif vol_regime == "low":
            score += 2
            bd["regime_score"] = "faible"
        elif vol_regime == "high":
            score += 4
            bd["regime_score"] = "eleve"
        elif vol_regime == "extreme":
            score += 0
            bd["regime_score"] = "extreme"
        else:
            score += 2
            bd["regime_score"] = "inconnu"

        # Bonus Bollinger Bands squeeze
        bb_upper = vals.get("bb_upper")
        bb_lower = vals.get("bb_lower")
        if all(v is not None for v in [bb_upper, bb_lower, close]):
            bb_width = (bb_upper - bb_lower) / close * 100
            if bb_width < 0.3:
                score += 5
                bd["bb_squeeze"] = True
            else:
                bd["bb_squeeze"] = False

        return score

    def _score_momentum(self, vals: Dict, bd: Dict) -> float:
        """
        Évalue le momentum via RSI et accélération. (0-12)
        Inclut la détection d'accélération du momentum.
        """
        score = 0.0
        rsi = vals.get("rsi")

        if rsi is None:
            bd["rsi"] = None
            return 0

        bd["rsi"] = round(rsi, 1)

        # RSI dans une zone de momentum sain (7 points max)
        if 40 <= rsi <= 60:
            score += 3
        elif 30 <= rsi <= 40:
            score += 6
        elif 60 <= rsi <= 70:
            score += 6
        elif rsi < 30:
            score += 5
        elif rsi > 70:
            score += 5

        # RSI non extrême = meilleur (3 points)
        if 35 <= rsi <= 65:
            score += 3

        # Detection d'acceleration du momentum (2 points, nouveau)
        accel = self._detect_momentum_acceleration(vals)
        bd["momentum_accel"] = accel
        if accel == "accelere_haussier":
            score += 2
        elif accel == "accelere_baissier":
            score += 2
        elif accel == "decelere":
            score += 0

        return score

    def _score_consistency(self, symbol: str, bd: Dict) -> float:
        """Bonus basé sur la régularité passée du marché. (0-10)"""
        scores = self.scan_history.get(symbol, [])
        if len(scores) < 3:
            bd["historique"] = "Insuffisant"
            return 4

        avg = float(np.mean(scores))
        recent_avg = float(np.mean(scores[-5:])) if len(scores) >= 5 else avg
        trend = recent_avg - avg

        bd["score_moyen"] = round(avg, 1)
        bd["trend_score"] = round(trend, 1)

        # Score moyen élevé
        if avg >= 60:
            score = 7
        elif avg >= 45:
            score = 4
        else:
            score = 1

        # Tendance à l'amélioration
        if trend > 5:
            score += 3
        elif trend > 0:
            score += 1

        return score

    # ------------------------------------------------------------------
    #  NOUVEAUX SCORES
    # ------------------------------------------------------------------

    def _score_session(self, bd: Dict) -> float:
        """
        Score basé sur la session de trading active. (0-8)
        Londres et New York sont preferees pour la liquidité.
        """
        try:
            now_utc = datetime.now(timezone.utc)
            hour = now_utc.hour
        except Exception:
            bd["session"] = "inconnue"
            return 4

        score = 0.0
        active_sessions = []

        for name, (start, end) in SESSION_WINDOWS.items():
            if start < end:
                is_active = start <= hour < end
            else:
                is_active = hour >= start or hour < end
            if is_active:
                active_sessions.append(name)

        bd["sessions_actives"] = active_sessions

        # Sessions preferees : London et NY
        if "london" in active_sessions and "new_york" in active_sessions:
            score = 8  # Chevauchement London/NY = meilleur
        elif "london" in active_sessions:
            score = 7
        elif "new_york" in active_sessions:
            score = 6
        elif "tokyo" in active_sessions:
            score = 4
        elif "sydney" in active_sessions:
            score = 3
        else:
            score = 2  # Hors session = risque eleve

        return score

    def _score_correlation(self, symbol: str, bd: Dict) -> float:
        """
        Filtre de corrélation pour éviter de trader des paires
        fortement corrélées simultanément. (0-8)

        Un score eleve signifie qu'il n'y a PAS de conflit de corrélation.
        """
        sym_upper = symbol.upper()

        # Trouver les paires corrélées
        correlated = CORRELATED_PAIRS.get(sym_upper, [])

        # Ajouter les paires ou ce symbole apparait comme corrélé
        for key, pairs in CORRELATED_PAIRS.items():
            if sym_upper in pairs and key not in correlated:
                correlated.append(key)

        # Verifier si une paire corrélée est deja active
        conflicts = []
        for corr_sym in correlated:
            if corr_sym in self.active_symbols and corr_sym != sym_upper:
                conflicts.append(corr_sym)

        bd["corrélés"] = correlated
        bd["conflits_corrélation"] = conflicts

        if len(conflicts) == 0:
            return 8  # Pas de conflit
        elif len(conflicts) == 1:
            return 4  # Un conflit, acceptable
        else:
            return 0  # Trop de conflits, eviter

    def _score_spread_liquidity(self, symbol: str, vals: Dict,
                                  price: float, bd: Dict) -> float:
        """
        Estime la qualité du spread et de la liquidité. (0-7)
        Les spreads faibles sont preferees pour un petit capital.
        """
        score = 0.0
        sym_upper = symbol.upper()

        # Determiner le type de symbole
        symbol_type = self._classify_symbol(sym_upper)
        bd["type_symbole"] = symbol_type

        # Score base sur le type (spread estimé)
        estimated_spread = ESTIMATED_SPREADS.get(symbol_type, 5.0)
        bd["spread_estimé"] = round(estimated_spread, 1)

        if estimated_spread <= 1.0:
            score += 4
        elif estimated_spread <= 3.0:
            score += 3
        elif estimated_spread <= 5.0:
            score += 2
        else:
            score += 0

        # Ratio spread/prix (3 points)
        if price > 0:
            spread_pct = (estimated_spread * 0.0001) / price * 100  # Pips en %
            bd["spread_pct"] = round(spread_pct, 4)
            if spread_pct < 0.01:
                score += 3
            elif spread_pct < 0.05:
                score += 2
            elif spread_pct < 0.1:
                score += 1

        return score

    def _score_recent_performance(self, symbol: str, bd: Dict) -> float:
        """
        Pénalise les symboles avec de récentes pertes importantes.
        Récompense les symboles avec des gains récents.
        """
        perf = self.recent_performance.get(symbol, [])
        if len(perf) < 2:
            bd["performance_récente"] = "insuffisante"
            return 0

        recent = perf[-5:] if len(perf) >= 5 else perf
        avg_pnl = float(np.mean(recent))
        worst = float(np.min(recent))

        bd["pnl_moyen_récent"] = round(avg_pnl, 2)
        bd["pire_trade_récent"] = round(worst, 2)

        # Pénalité si pertes récentes
        if avg_pnl < -2.0:
            return -5  # Forte pénalité
        elif avg_pnl < -1.0:
            return -3
        elif avg_pnl < 0:
            return -1
        elif avg_pnl > 1.0:
            return 2
        else:
            return 0

    # ------------------------------------------------------------------
    #  METHODES UTILITAIRES
    # ------------------------------------------------------------------

    def _detect_momentum_acceleration(self, vals: Dict) -> str:
        """
        Détecte si le momentum est en train d'accélérer ou de décélérer.
        Compare les indicateurs de momentum (RSI, MACD, Stochastique)
        entre les valeurs actuelles et précédentes.

        Returns:
            'accelere_haussier' : le momentum haussier augmente
            'accelere_baissier' : le momentum baissier augmente
            'decelere' : le momentum diminue
            'stable' : pas de changement significatif
        """
        rsi = vals.get("rsi")
        rsi_prev = vals.get("rsi_prev")
        macd_hist = vals.get("macd_hist")
        macd_hist_prev = vals.get("macd_hist_prev")
        stoch_k = vals.get("stoch_k")
        stoch_k_prev = vals.get("stoch_k_prev")

        signals = []

        # RSI acceleration
        if rsi is not None and rsi_prev is not None:
            rsi_diff = rsi - rsi_prev
            if rsi_diff > 3:
                signals.append(1)
            elif rsi_diff < -3:
                signals.append(-1)

        # MACD histogramme acceleration
        if macd_hist is not None and macd_hist_prev is not None:
            macd_diff = macd_hist - macd_hist_prev
            if macd_diff > 0 and macd_hist > 0:
                signals.append(1)
            elif macd_diff < 0 and macd_hist < 0:
                signals.append(-1)
            elif macd_diff > 0 and macd_hist < 0:
                signals.append(0.5)  # Convergence
            elif macd_diff < 0 and macd_hist > 0:
                signals.append(-0.5)

        # Stochastique acceleration
        if stoch_k is not None and stoch_k_prev is not None:
            stoch_diff = stoch_k - stoch_k_prev
            if stoch_diff > 5:
                signals.append(1)
            elif stoch_diff < -5:
                signals.append(-1)

        if len(signals) == 0:
            return "stable"

        avg_signal = float(np.mean(signals))

        if avg_signal > 0.5:
            return "accelere_haussier"
        elif avg_signal < -0.5:
            return "accelere_baissier"
        elif abs(avg_signal) < 0.2:
            return "decelere"
        else:
            return "stable"

    @staticmethod
    def _classify_symbol(symbol: str) -> str:
        """
        Classifie un symbole en categorie pour estimer le spread.

        Returns:
            'major', 'minor', 'exotic', 'synthetic', 'metal', 'index'
        """
        sym = symbol.upper()

        majors = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
        if sym in majors:
            return "major"

        metals = ["XAUUSD", "XAGUSD", "GOLD", "SILVER"]
        if any(m in sym for m in metals):
            return "metal"

        if "VOL" in sym or "CRASH" in sym or "BOOM" in sym or "STEP" in sym:
            return "synthetic"

        if "US30" in sym or "NAS" in sym or "SPX" in sym or "DAX" in sym or "FTSE" in sym:
            return "index"

        minors = ["EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY",
                   "EURAUD", "EURNZD", "GBPAUD", "GBPNZD", "AUDNZD",
                   "AUDCAD", "NZDCAD", "CHFJPY"]
        if sym in minors:
            return "minor"

        if "USD" in sym:
            return "minor"

        return "exotic"

    def _save_score(self, symbol: str, score: float) -> None:
        if symbol not in self.scan_history:
            self.scan_history[symbol] = []
        self.scan_history[symbol].append(score)
        if len(self.scan_history[symbol]) > self.max_history:
            self.scan_history[symbol] = self.scan_history[symbol][-self.max_history:]

    def get_market_report(self) -> Dict:
        """Retourne un rapport de tous les marchés analysés."""
        report = {}
        for symbol, scores in self.scan_history.items():
            if scores:
                report[symbol] = {
                    "current_score": scores[-1],
                    "avg_score": round(float(np.mean(scores)), 1),
                    "max_score": round(float(np.max(scores)), 1),
                    "min_score": round(float(np.min(scores)), 1),
                    "evaluations": len(scores),
                }
        return report
