"""
Analyse Multi-Timeframe v3.
======================================================
Confirme un signal M5 avec M15, M30, H1 et optionnellement H4.

Ameliorations v3 :
  1. Analyse de la STRUCTURE granulaire du marche (HH/HL/LH/LL)
  2. Score de confluence PONDERE (H1/H4 comptent plus que M15/M30)
  3. Mode "confirmation partielle" : reduit la mise au lieu de bloquer
  4. Analyse du MOMENTUM sur chaque TF (RSI + histogramme MACD)
  5. Detection des DIVERGENCES RSI multi-TF
  6. Detection des DIVERGENCES MACD (momentum) multi-TF
  7. Confirmation par VOLUME PROFILE (si donnees disponibles)
  8. ALIGNEMENT DES TENDANCES (score d'alignement directionnel)
  9. Notation de QUALITE DE CONFLUENCE (parfaite/bonne/partielle/conflictuelle)
 10. Multiplicateur de LOT ADAPTATIF (plus d'accord = mise plus grande)
 11. Support M30 et H4 en plus de M15/H1
 12. Detection de NIVEAUX SUPPORT/RESISTANCE croises entre TFs
 13. Gestion robuste : fallback si un TF echoue
 14. Journalisation francaise detaillee

Principe :
  - Signal M5 = direction potentielle
  - M15/M30 confirme la meme direction = signal renforce
  - H1/H4 confirme aussi = signal tres fort
  - Contradiction sur un TF = signal annule OU mise reduite

Score de confluence :
  - 0 TF agree     = 0.0 (signal annule ou mise x0.25)
  - 1 TF agree     = 0.7 (signal faible, mise x0.5)
  - 2 TF agree     = 1.2 (signal bon)
  - 3+ TF agree    = 2.0 (signal tres fort, mise x1.2)

Qualite de confluence :
  - "parfaite" : tous les TFs alignes, divergence AUCUNE, structure OK
  - "bonne"   : plupart des TFs d'accord, 1 seul en desaccord mineur
  - "partielle": quelques TFs d'accord, d'autres neutres
  - "conflictuelle" : TFs en contradiction forte

Mode de confirmation :
  - "strict"  : bloquer si pas de confirmation complete
  - "partial" : reduire la mise proportionnellement au score
  - "soft"    : avertir mais ne pas bloquer
"""

import logging
import copy
import numpy as np
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Mapping des timeframes Deriv en secondes
TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900,
    "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400,
}

# Poids par timeframe (les TF plus longs sont plus importants)
TF_WEIGHTS = {
    "M1": 0.5, "M5": 1.0, "M15": 1.5,
    "M30": 2.0, "H1": 2.5, "H4": 3.0, "D1": 4.0,
}

# Multiplicateurs d'indicateurs pour adapter aux TF plus longs
TF_INDICATOR_MULT = {
    "M15": 1.5, "M30": 2.0, "H1": 3.0, "H4": 5.0, "D1": 8.0,
}

# Noms de qualite de confluence en francais
CONFLUENCE_QUALITY_LABELS = {
    "parfaite": "PARFAITE",
    "bonne": "BONNE",
    "partielle": "PARTIELLE",
    "conflictuelle": "CONFLICTUELLE",
}


class MultiTimeframeAnalyzer:
    """Confirme les signaux sur plusieurs timeframes avec analyse structurelle avancee."""

    def __init__(self, config: dict):
        self.config = config
        mtf = config.get("multi_timeframe", {})
        self.enabled = mtf.get("enabled", True)
        self.base_tf = config["trading"]["timeframe"]
        self.confirm_tfs = mtf.get("confirm_timeframes", ["M15", "M30", "H1"])
        self.min_agreement = mtf.get("min_agreement", 1)
        self.bars_per_tf = mtf.get("bars_per_timeframe", 300)
        self.mode = mtf.get("mode", "strict")  # strict / partial / soft
        self.use_structure = mtf.get("use_structure", True)
        self.use_momentum = mtf.get("use_momentum", True)
        self.lot_multiplier = mtf.get("lot_multiplier", True)
        self.use_divergence = mtf.get("use_divergence", True)
        self.use_volume = mtf.get("use_volume", True)
        self.use_sr_levels = mtf.get("use_sr_levels", True)
        self.use_macd_divergence = mtf.get("use_macd_divergence", True)

    def analyze(self, connector, symbol: str,
                base_analysis: Dict, base_latest: Dict,
                base_signal: str) -> Dict:
        """
        Analyse multi-timeframe complete avec structure, momentum, divergences et S/R.

        Returns:
            {
                'confirmed': bool,
                'signal': 'BUY'|'SELL'|'HOLD',
                'confluence_score': float (0-2.5+),
                'lot_multiplier': float (0.25 - 1.5),
                'confluence_quality': str (parfaite/bonne/partielle/conflictuelle),
                'trend_alignment': float (0-1),
                'timeframe_results': {tf: {...}},
                'details': str,
                'structure_score': float,
                'momentum_score': float,
                'rsi_divergence': dict,
                'macd_divergence': dict,
                'volume_confirmation': float,
                'sr_levels': list,
            }
        """
        if not self.enabled:
            return self._disabled_result(base_signal)

        if base_signal == "HOLD":
            return {
                "confirmed": False, "signal": "HOLD",
                "confluence_score": 0.0, "lot_multiplier": 0.25,
                "timeframe_results": {},
                "details": "Signal de base = HOLD",
                "structure_score": 0.0, "momentum_score": 0.0,
                "confluence_quality": "conflictuelle",
                "trend_alignment": 0.0,
                "rsi_divergence": {"detected": False, "type": None, "strength": 0.0},
                "macd_divergence": {"detected": False, "type": None, "strength": 0.0},
                "volume_confirmation": 0.5,
                "sr_levels": [],
            }

        results = {}
        weighted_agreement = 0.0
        total_possible_weight = 0.0
        agreement_count = 0
        disagreement_count = 0
        structure_scores = []
        momentum_scores = []
        tf_directions = []
        all_sr_levels = []
        rsi_divergences = []
        macd_divergences = []
        volume_scores = []

        logger.debug(
            f"[Multi-TF] Demarrage analyse {symbol} | Signal base: {base_signal} | "
            f"TFs confirmation: {', '.join(self.confirm_tfs)}"
        )

        for tf in self.confirm_tfs:
            try:
                tf_result = self._analyze_timeframe(
                    connector, symbol, tf, base_signal
                )
                results[tf] = tf_result

                tf_weight = TF_WEIGHTS.get(tf, 1.0)
                total_possible_weight += tf_weight

                if tf_result["agree"]:
                    agreement_count += 1
                    weighted_agreement += tf_weight * tf_result["score"]
                    tf_directions.append(1)
                elif tf_result.get("direction") in ("BUY", "SELL"):
                    disagreement_count += 1
                    tf_directions.append(-1)
                else:
                    tf_directions.append(0)

                if self.use_structure:
                    structure_scores.append(tf_result.get("structure_score", 0.5))
                if self.use_momentum:
                    momentum_scores.append(tf_result.get("momentum_score", 0.5))

                # Collecter les niveaux S/R de chaque TF
                if self.use_sr_levels:
                    sr = tf_result.get("sr_levels", [])
                    all_sr_levels.extend(sr)

                # Collecter les divergences RSI
                if self.use_divergence:
                    rsi_div = tf_result.get("rsi_divergence", {})
                    if rsi_div.get("detected", False):
                        rsi_divergences.append({**rsi_div, "timeframe": tf})

                # Collecter les divergences MACD
                if self.use_macd_divergence:
                    macd_div = tf_result.get("macd_divergence", {})
                    if macd_div.get("detected", False):
                        macd_divergences.append({**macd_div, "timeframe": tf})

                # Confirmation par volume
                if self.use_volume:
                    vol_conf = tf_result.get("volume_confirmation", 0.5)
                    volume_scores.append(vol_conf)

                logger.debug(
                    f"[Multi-TF] {symbol} | TF {tf}: direction={tf_result['direction']} "
                    f"score={tf_result['score']:.2f} accord={tf_result['agree']} "
                    f"structure={tf_result.get('structure_score', 0):.2f} "
                    f"momentum={tf_result.get('momentum_score', 0):.2f}"
                )

            except Exception as e:
                logger.debug(f"[Multi-TF] Analyse {tf} echouee pour {symbol}: {e}")
                results[tf] = {
                    "direction": "?", "score": 0, "agree": False,
                    "error": str(e), "structure_score": 0, "momentum_score": 0,
                    "rsi_divergence": {"detected": False, "type": None, "strength": 0.0},
                    "macd_divergence": {"detected": False, "type": None, "strength": 0.0},
                    "volume_confirmation": 0.5, "sr_levels": [],
                }
                tf_directions.append(0)

        # --- Score de confluence pondere (0 a 2.5) ---
        if total_possible_weight > 0:
            confluence = weighted_agreement / total_possible_weight * 2.0
        else:
            confluence = 0.0

        # Bonus structure
        avg_structure = float(np.mean(structure_scores)) if structure_scores else 0.5
        structure_bonus = (avg_structure - 0.5) * 0.3 if self.use_structure else 0
        confluence += structure_bonus

        # Bonus momentum
        avg_momentum = float(np.mean(momentum_scores)) if momentum_scores else 0.5
        momentum_bonus = (avg_momentum - 0.5) * 0.2 if self.use_momentum else 0
        confluence += momentum_bonus

        # --- Alignment des tendances ---
        trend_alignment = self._compute_trend_alignment(
            tf_directions, base_signal, len(self.confirm_tfs)
        )
        alignment_bonus = (trend_alignment - 0.5) * 0.2
        confluence += alignment_bonus

        # --- Divergences RSI (penalisation si divergence contre le signal) ---
        rsi_div_summary = self._aggregate_divergences(rsi_divergences, base_signal)
        if rsi_div_summary["penalty"] > 0:
            confluence -= rsi_div_summary["penalty"]
            logger.debug(
                f"[Multi-TF] {symbol} | Divergence RSI detectee: "
                f"penalite={rsi_div_summary['penalty']:.2f}"
            )

        # --- Divergences MACD (penalisation si divergence contre le signal) ---
        macd_div_summary = self._aggregate_divergences(macd_divergences, base_signal)
        if macd_div_summary["penalty"] > 0:
            confluence -= macd_div_summary["penalty"]
            logger.debug(
                f"[Multi-TF] {symbol} | Divergence MACD detectee: "
                f"penalite={macd_div_summary['penalty']:.2f}"
            )

        # --- Confirmation par volume ---
        avg_volume = float(np.mean(volume_scores)) if volume_scores else 0.5
        volume_bonus = (avg_volume - 0.5) * 0.15
        confluence += volume_bonus

        confluence = max(0.0, min(2.5, confluence))

        # --- Qualite de confluence ---
        confluence_quality = self._rate_confluence_quality(
            agreement_count, disagreement_count, len(self.confirm_tfs),
            confluence, rsi_divergences, macd_divergences
        )

        # --- Niveaux S/R consolides ---
        consolidated_sr = self._consolidate_sr_levels(all_sr_levels)

        # --- Determiner la confirmation selon le mode ---
        confirmed, lot_mult, detail = self._evaluate_confluence(
            agreement_count, confluence, base_signal, len(self.confirm_tfs)
        )

        # Multiplicateur de lot adaptatif: plus d'accord = mise plus grande
        if self.lot_multiplier:
            lot_mult = self._adaptive_lot_multiplier(
                agreement_count, len(self.confirm_tfs), confluence, confluence_quality
            )

        if not confirmed and self.mode == "partial":
            lot_mult = max(0.25, confluence / 2.0)
            confirmed = confluence >= 0.5
            detail += f" | Mode PARTIEL: mise x{lot_mult:.2f}"
        elif not confirmed and self.mode == "soft":
            lot_mult = max(0.5, confluence / 2.0)
            confirmed = True
            detail += " | Mode SOFT: trade autorise avec mise reduite"

        final_signal = base_signal if confirmed else "HOLD"

        quality_label = CONFLUENCE_QUALITY_LABELS.get(confluence_quality, confluence_quality)
        logger.info(
            f"[Multi-TF] {symbol} | {detail} | Confluence={confluence:.2f} | "
            f"Qualite={quality_label} | Alignement={trend_alignment:.2f} | "
            f"Lot x{lot_mult:.2f} | Accords={agreement_count}/{len(self.confirm_tfs)}"
        )

        return {
            "confirmed": confirmed,
            "signal": final_signal,
            "confluence_score": round(confluence, 3),
            "lot_multiplier": round(lot_mult, 2),
            "confluence_quality": confluence_quality,
            "trend_alignment": round(trend_alignment, 3),
            "timeframe_results": results,
            "details": detail,
            "structure_score": round(avg_structure, 3) if structure_scores else 0.0,
            "momentum_score": round(avg_momentum, 3) if momentum_scores else 0.0,
            "rsi_divergence": rsi_div_summary,
            "macd_divergence": macd_div_summary,
            "volume_confirmation": round(avg_volume, 3) if volume_scores else 0.5,
            "sr_levels": consolidated_sr,
        }

    def _rate_confluence_quality(self, agreement_count: int, disagreement_count: int,
                                  total_tfs: int, confluence: float,
                                  rsi_divs: List[Dict], macd_divs: List[Dict]) -> str:
        """
        Evalue la qualite de la confluence.
        Retourne: 'parfaite', 'bonne', 'partielle' ou 'conflictuelle'.
        """
        total_divergences = len(rsi_divs) + len(macd_divs)
        ratio = agreement_count / total_tfs if total_tfs > 0 else 0

        # Parfaite: tous d'accord, pas de divergence, confluence elevee
        if (agreement_count == total_tfs and total_divergences == 0
                and confluence >= 1.8):
            return "parfaite"
        # Bonne: majorite d'accord, peu de divergences
        if ratio >= 0.7 and total_divergences <= 1 and confluence >= 1.2:
            return "bonne"
        # Partielle: certains d'accord, pas trop de conflits
        if ratio >= 0.4 and disagreement_count < agreement_count:
            return "partielle"
        # Conflictuelle: beaucoup de desaccords ou divergences
        return "conflictuelle"

    def _compute_trend_alignment(self, tf_directions: List[int],
                                  base_signal: str, total_tfs: int) -> float:
        """
        Calcule le score d'alignement des tendances (0 a 1).
        1.0 = tous les TFs pointent dans la meme direction que le signal.
        0.0 = tous les TFs pointent dans la direction opposee.
        0.5 = neutre ou indecis.
        """
        if not tf_directions:
            return 0.5

        favorable = sum(1 for d in tf_directions if d == 1)
        unfavorable = sum(1 for d in tf_directions if d == -1)
        neutral = sum(1 for d in tf_directions if d == 0)

        total = len(tf_directions)
        if total == 0:
            return 0.5

        # Score base sur le ratio favorable vs defavorable
        if favorable + unfavorable == 0:
            return 0.5

        alignment = favorable / (favorable + unfavorable)
        return round(alignment, 3)

    def _adaptive_lot_multiplier(self, agreement_count: int, total_tfs: int,
                                   confluence: float, quality: str) -> float:
        """
        Multiplicateur de lot adaptatif base sur le nombre de TFs d'accord.
        Plus d'accord = mise plus grande (jusqu'a 1.5x).
        Moins d'accord = mise plus petite (jusqu'a 0.25x).
        """
        ratio = agreement_count / total_tfs if total_tfs > 0 else 0

        if quality == "parfaite":
            return 1.5
        elif quality == "bonne":
            return min(1.5, 0.8 + ratio * 0.7)
        elif quality == "partielle":
            return max(0.5, 0.5 + ratio * 0.5)
        else:  # conflictuelle
            return max(0.25, 0.3 + ratio * 0.3)

    def _aggregate_divergences(self, divergences: List[Dict],
                                base_signal: str) -> Dict:
        """
        Agrège les divergences detectees sur tous les TFs.
        Retourne un resume avec penalite si divergence contre le signal.
        """
        if not divergences:
            return {"detected": False, "type": None, "strength": 0.0, "penalty": 0.0, "count": 0}

        bullish_divs = [d for d in divergences if d.get("type") == "haussiere"]
        bearish_divs = [d for d in divergences if d.get("type") == "baissiere"]

        # Penalite si divergence contre le signal de base
        penalty = 0.0
        dominant_type = None
        if base_signal == "BUY" and bearish_divs:
            penalty = min(0.5, len(bearish_divs) * 0.15)
            dominant_type = "baissiere"
        elif base_signal == "SELL" and bullish_divs:
            penalty = min(0.5, len(bullish_divs) * 0.15)
            dominant_type = "haussiere"
        elif bullish_divs:
            dominant_type = "haussiere"
        elif bearish_divs:
            dominant_type = "baissiere"

        strengths = [d.get("strength", 0) for d in divergences]
        avg_strength = float(np.mean(strengths)) if strengths else 0.0

        return {
            "detected": True,
            "type": dominant_type,
            "strength": round(avg_strength, 3),
            "penalty": round(penalty, 3),
            "count": len(divergences),
            "details": [
                f"{d['timeframe']}: {d['type']} (force={d.get('strength', 0):.2f})"
                for d in divergences
            ],
        }

    def _detect_rsi_divergence(self, ohlc: Dict, rsi_values: list,
                                signal: str, lookback: int = 30) -> Dict:
        """
        Detecte les divergences RSI: prix fait un nouveau haut/bas
        mais le RSI ne confirme pas.
        Retourne: {detected, type, strength}
        """
        closes = ohlc["close"]
        n = min(lookback, len(closes))
        if n < 15 or len(rsi_values) < n:
            return {"detected": False, "type": None, "strength": 0.0}

        recent_close = closes[-n:]
        recent_rsi = rsi_values[-n:]

        # Trouver les extremums de prix (simplifie)
        half = n // 2
        first_high = max(recent_close[:half])
        second_high = max(recent_close[half:])
        first_low = min(recent_close[:half])
        second_low = min(recent_close[half:])

        first_rsi_high = max(recent_rsi[:half])
        second_rsi_high = max(recent_rsi[half:])
        first_rsi_low = min(recent_rsi[:half])
        second_rsi_low = min(recent_rsi[half:])

        # Divergence haussiere: prix fait lower low, RSI fait higher low
        if (second_low < first_low and second_rsi_low > first_rsi_low):
            strength = min(1.0, abs(second_rsi_low - first_rsi_low) / 10.0)
            return {"detected": True, "type": "haussiere", "strength": round(strength, 3)}

        # Divergence baissiere: prix fait higher high, RSI fait lower high
        if (second_high > first_high and second_rsi_high < first_rsi_high):
            strength = min(1.0, abs(first_rsi_high - second_rsi_high) / 10.0)
            return {"detected": True, "type": "baissiere", "strength": round(strength, 3)}

        return {"detected": False, "type": None, "strength": 0.0}

    def _detect_macd_divergence(self, ohlc: Dict, macd_line: list,
                                 macd_hist: list, signal: str,
                                 lookback: int = 30) -> Dict:
        """
        Detecte les divergences MACD: prix fait un nouveau haut/bas
        mais le MACD/histogramme ne confirme pas.
        """
        closes = ohlc["close"]
        n = min(lookback, len(closes))
        if n < 15 or len(macd_hist) < n:
            return {"detected": False, "type": None, "strength": 0.0}

        recent_close = closes[-n:]
        recent_hist = macd_hist[-n:]

        half = n // 2
        first_high = max(recent_close[:half])
        second_high = max(recent_close[half:])
        first_low = min(recent_close[:half])
        second_low = min(recent_close[half:])

        first_hist_high = max(recent_hist[:half])
        second_hist_high = max(recent_hist[half:])
        first_hist_low = min(recent_hist[:half])
        second_hist_low = min(recent_hist[half:])

        # Divergence haussiere: prix lower low, histogramme higher low
        if (second_low < first_low and second_hist_low > first_hist_low):
            strength = min(1.0, abs(second_hist_low - first_hist_low) / (abs(first_hist_low) + 1e-10))
            return {"detected": True, "type": "haussiere", "strength": round(min(1.0, strength), 3)}

        # Divergence baissiere: prix higher high, histogramme lower high
        if (second_high > first_high and second_hist_high < first_hist_high):
            strength = min(1.0, abs(first_hist_high - second_hist_high) / (abs(first_hist_high) + 1e-10))
            return {"detected": True, "type": "baissiere", "strength": round(min(1.0, strength), 3)}

        return {"detected": False, "type": None, "strength": 0.0}

    def _analyze_volume_profile(self, ohlc: Dict, signal: str) -> float:
        """
        Analyse le profil de volume pour confirmer le signal.
        Score: 1.0 = volume confirme fortement, 0.0 = volume contraire.
        """
        if "volume" not in ohlc or ohlc["volume"] is None:
            return 0.5  # Neutre si pas de donnees de volume

        volumes = np.array(ohlc["volume"], dtype=float)
        closes = np.array(ohlc["close"], dtype=float)

        n = min(50, len(volumes))
        if n < 10:
            return 0.5

        recent_vol = volumes[-n:]
        recent_close = closes[-n:]

        # Volume moyen recente vs historique
        avg_vol_recent = np.mean(recent_vol[:n // 2])
        avg_vol_current = np.mean(recent_vol[n // 2:])

        # Variation de prix
        price_change = recent_close[-1] - recent_close[0]
        price_moving_up = price_change > 0

        score = 0.5

        # Volume en augmentation quand le prix monte = haussier confirme
        if avg_vol_current > avg_vol_recent * 1.1:
            if price_moving_up:
                score += 0.3  # Volume croissant + prix montant = fort
            else:
                score -= 0.2  # Volume croissant + prix descendant = vente forte
        elif avg_vol_current < avg_vol_recent * 0.9:
            if price_moving_up:
                score -= 0.1  # Volume decroissant + prix montant = faible
            else:
                score += 0.1  # Volume decroissant + prix descendant = essoufflement

        # Confirmation directionnelle du volume
        if signal == "BUY" and price_moving_up and avg_vol_current > avg_vol_recent:
            score += 0.2
        elif signal == "SELL" and not price_moving_up and avg_vol_current > avg_vol_recent:
            score += 0.2

        return max(0.0, min(1.0, score))

    def _detect_sr_levels(self, ohlc: Dict) -> List[Dict]:
        """
        Detecte les niveaux de support et resistance a partir des
        swing highs et swing lows.
        Retourne une liste de {level, type, strength}.
        """
        closes = np.array(ohlc["close"], dtype=float)
        highs = np.array(ohlc["high"], dtype=float)
        lows = np.array(ohlc["low"], dtype=float)

        n = min(200, len(closes))
        if n < 20:
            return []

        recent_highs = highs[-n:]
        recent_lows = lows[-n:]
        recent_closes = closes[-n:]

        levels = []
        window = max(3, n // 20)

        # Detecter les swing highs (resistances)
        for i in range(window, len(recent_highs) - window):
            is_swing_high = True
            for j in range(1, window + 1):
                if recent_highs[i] <= recent_highs[i - j] or recent_highs[i] <= recent_highs[i + j]:
                    is_swing_high = False
                    break
            if is_swing_high:
                levels.append({
                    "price": round(float(recent_highs[i]), 5),
                    "type": "resistance",
                    "strength": round(float(recent_highs[i] - np.min(recent_lows)) / (np.max(recent_highs) - np.min(recent_lows) + 1e-10), 3),
                })

        # Detecter les swing lows (supports)
        for i in range(window, len(recent_lows) - window):
            is_swing_low = True
            for j in range(1, window + 1):
                if recent_lows[i] >= recent_lows[i - j] or recent_lows[i] >= recent_lows[i + j]:
                    is_swing_low = False
                    break
            if is_swing_low:
                levels.append({
                    "price": round(float(recent_lows[i]), 5),
                    "type": "support",
                    "strength": round(float(np.max(recent_highs) - recent_lows[i]) / (np.max(recent_highs) - np.min(recent_lows) + 1e-10), 3),
                })

        # Dedoublonner les niveaux proches (seuil 0.1%)
        consolidated = []
        for level in sorted(levels, key=lambda x: x["strength"], reverse=True):
            is_duplicate = False
            for existing in consolidated:
                if abs(level["price"] - existing["price"]) / (existing["price"] + 1e-10) < 0.001:
                    is_duplicate = True
                    break
            if not is_duplicate:
                consolidated.append(level)

        return consolidated[:10]  # Garder les 10 plus forts

    def _consolidate_sr_levels(self, all_levels: List[Dict]) -> List[Dict]:
        """
        Consolide les niveaux S/R de plusieurs timeframes.
        Un niveau renforce par plusieurs TFs obtient un score plus eleve.
        """
        if not all_levels:
            return []

        # Grouper les niveaux proches
        clusters = []
        for level in all_levels:
            merged = False
            for cluster in clusters:
                if abs(level["price"] - cluster["price"]) / (cluster["price"] + 1e-10) < 0.002:
                    cluster["count"] += 1
                    cluster["strength"] = max(cluster["strength"], level["strength"])
                    merged = True
                    break
            if not merged:
                clusters.append({
                    "price": level["price"],
                    "type": level["type"],
                    "strength": level["strength"],
                    "count": 1,
                })

        # Trier par nombre de confirmations puis par force
        clusters.sort(key=lambda x: (x["count"], x["strength"]), reverse=True)
        return clusters[:8]

    def _evaluate_confluence(self, agreement_count: int, confluence: float,
                               base_signal: str, total_tfs: int) -> Tuple[bool, float, str]:
        """Evalue le niveau de confluence."""
        if agreement_count == 0:
            return False, 0.25, (
                f"Signal {base_signal} ANNULE: 0/{total_tfs} TF confirment"
            )
        elif confluence >= 2.0:
            return True, 1.5, (
                f"Signal {base_signal} TRES FORT: {agreement_count}/{total_tfs} TF "
                f"(confluence={confluence:.2f})"
            )
        elif confluence >= 1.2:
            return True, 1.2, (
                f"Signal {base_signal} FORT: {agreement_count}/{total_tfs} TF "
                f"(confluence={confluence:.2f})"
            )
        elif confluence >= 0.7:
            return True, 1.0, (
                f"Signal {base_signal} confirme: {agreement_count}/{total_tfs} TF "
                f"(confluence={confluence:.2f})"
            )
        else:
            return False, 0.5, (
                f"Signal {base_signal} FAIBLE: {agreement_count}/{total_tfs} TF "
                f"(confluence={confluence:.2f})"
            )

    def _analyze_timeframe(self, connector, symbol: str,
                            timeframe: str, base_signal: str) -> Dict:
        """Analyse un seul timeframe superieur avec structure + momentum + divergences."""
        from technical_analysis import TechnicalAnalysis
        from strategy_engine import StrategyEngine

        ohlc = self._get_tf_data(connector, symbol, timeframe)
        if ohlc is None:
            return {"direction": "N/A", "score": 0, "agree": False,
                    "structure_score": 0, "momentum_score": 0,
                    "rsi_divergence": {"detected": False, "type": None, "strength": 0.0},
                    "macd_divergence": {"detected": False, "type": None, "strength": 0.0},
                    "volume_confirmation": 0.5, "sr_levels": []}

        tf_config = self._make_tf_config(timeframe)
        ta = TechnicalAnalysis(tf_config)
        strategy = StrategyEngine(tf_config)

        analysis = ta.full_analysis(ohlc)
        latest = ta.get_latest_values(analysis)
        prices = connector.get_current_price(symbol)
        if prices is None:
            return {"direction": "N/A", "score": 0, "agree": False,
                    "structure_score": 0, "momentum_score": 0,
                    "rsi_divergence": {"detected": False, "type": None, "strength": 0.0},
                    "macd_divergence": {"detected": False, "type": None, "strength": 0.0},
                    "volume_confirmation": 0.5, "sr_levels": []}
        current_price = prices[1]

        result = strategy.generate_signal(latest, current_price)
        tf_signal = result["signal"]
        tf_score = abs(result["total_score"])
        agree = (tf_signal == base_signal)

        # Analyse de la structure granulaire du marche (HH/HL/LH/LL)
        structure_score = 0.5
        if self.use_structure and ohlc is not None:
            structure_score = self._analyze_structure(ohlc, base_signal)

        # Analyse du momentum
        momentum_score = 0.5
        if self.use_momentum:
            momentum_score = self._analyze_momentum(latest)

        # Divergence RSI
        rsi_div = {"detected": False, "type": None, "strength": 0.0}
        if self.use_divergence:
            rsi_values = analysis.get("rsi", [])
            if rsi_values:
                rsi_div = self._detect_rsi_divergence(ohlc, rsi_values, base_signal)

        # Divergence MACD
        macd_div = {"detected": False, "type": None, "strength": 0.0}
        if self.use_macd_divergence:
            macd_line = analysis.get("macd_line", [])
            macd_hist = analysis.get("macd_hist", [])
            if macd_line and macd_hist:
                macd_div = self._detect_macd_divergence(
                    ohlc, macd_line, macd_hist, base_signal
                )

        # Volume profile
        vol_conf = 0.5
        if self.use_volume:
            vol_conf = self._analyze_volume_profile(ohlc, base_signal)

        # Niveaux S/R
        sr_levels = []
        if self.use_sr_levels:
            sr_levels = self._detect_sr_levels(ohlc)

        return {
            "direction": tf_signal,
            "score": tf_score,
            "agree": agree,
            "confidence": result["confidence"],
            "structure_score": structure_score,
            "momentum_score": momentum_score,
            "rsi_divergence": rsi_div,
            "macd_divergence": macd_div,
            "volume_confirmation": vol_conf,
            "sr_levels": sr_levels,
        }

    def _analyze_structure(self, ohlc: Dict, signal: str) -> float:
        """
        Analyse granulaire de la structure du marche: HH/HL/LH/LL.
        Plus de segments = analyse plus precise.

        Score: 1.0 = structure parfaite, 0.0 = structure opposee, 0.5 = neutre.
        """
        closes = ohlc["close"]
        highs = ohlc["high"]
        lows = ohlc["low"]
        n = min(80, len(closes))
        if n < 15:
            return 0.5

        recent_closes = closes[-n:]
        recent_highs = highs[-n:]
        recent_lows = lows[-n:]

        # Detecter les swing points pour analyser HH/HL/LH/LL
        swing_highs = self._find_swing_points(recent_highs, "high")
        swing_lows = self._find_swing_points(recent_lows, "low")

        if len(swing_highs) < 2 and len(swing_lows) < 2:
            # Fallback: analyse par moities (ancienne methode)
            mid = n // 2
            first_half_high = np.mean(recent_highs[:mid])
            second_half_high = np.mean(recent_highs[mid:])
            first_half_low = np.mean(recent_lows[:mid])
            second_half_low = np.mean(recent_lows[mid:])

            bullish_structure = (
                second_half_high > first_half_high and
                second_half_low > first_half_low
            )
            bearish_structure = (
                second_half_high < first_half_high and
                second_half_low < first_half_low
            )

            if signal == "BUY":
                if bullish_structure:
                    return 0.85
                elif bearish_structure:
                    return 0.25
            elif signal == "SELL":
                if bearish_structure:
                    return 0.85
                elif bullish_structure:
                    return 0.25
            return 0.5

        # Compter les patterns HH/HL/LH/LL
        hh_count = 0
        hl_count = 0
        lh_count = 0
        ll_count = 0

        for i in range(1, len(swing_highs)):
            if swing_highs[i] > swing_highs[i - 1]:
                hh_count += 1
            else:
                lh_count += 1

        for i in range(1, len(swing_lows)):
            if swing_lows[i] > swing_lows[i - 1]:
                hl_count += 1
            else:
                ll_count += 1

        total_highs = hh_count + lh_count
        total_lows = hl_count + ll_count

        # Score de structure
        score = 0.5
        if signal == "BUY":
            # Haussier: HH + HL
            if total_highs > 0:
                score += (hh_count / total_highs - 0.5) * 0.3
            if total_lows > 0:
                score += (hl_count / total_lows - 0.5) * 0.3
            # Bonus si les deux confirment
            if total_highs > 0 and total_lows > 0:
                if (hh_count / total_highs > 0.6 and hl_count / total_lows > 0.6):
                    score += 0.2
        elif signal == "SELL":
            # Baissier: LH + LL
            if total_highs > 0:
                score += (lh_count / total_highs - 0.5) * 0.3
            if total_lows > 0:
                score += (ll_count / total_lows - 0.5) * 0.3
            # Bonus si les deux confirment
            if total_highs > 0 and total_lows > 0:
                if (lh_count / total_highs > 0.6 and ll_count / total_lows > 0.6):
                    score += 0.2

        return max(0.0, min(1.0, score))

    def _find_swing_points(self, prices: list, point_type: str) -> List[float]:
        """
        Trouve les swing points (highs ou lows) dans une serie de prix.
        Utilise une fenetre glissante de 3 bougies pour detecter les pivots.
        """
        if len(prices) < 5:
            return []

        swings = []
        window = 3

        for i in range(window, len(prices) - window):
            is_swing = True
            for j in range(1, window + 1):
                if point_type == "high":
                    if prices[i] <= prices[i - j] or prices[i] <= prices[i + j]:
                        is_swing = False
                        break
                else:  # low
                    if prices[i] >= prices[i - j] or prices[i] >= prices[i + j]:
                        is_swing = False
                        break
            if is_swing:
                swings.append(float(prices[i]))

        return swings

    def _analyze_momentum(self, latest: Dict) -> float:
        """
        Analyse le momentum: RSI + MACD histogramme.
        Score: 1.0 = momentum fort haussier, 0.0 = fort baissier.
        """
        rsi = latest.get("rsi", 50)
        macd_hist = latest.get("macd_hist", 0)
        adx = latest.get("adx", 0)

        score = 0.5

        # RSI momentum
        if rsi > 55:
            score += 0.15
        elif rsi < 45:
            score -= 0.15
        if rsi > 65:
            score += 0.1
        elif rsi < 35:
            score -= 0.1

        # MACD histogramme momentum
        if macd_hist > 0:
            score += 0.1
        elif macd_hist < 0:
            score -= 0.1

        # Force de tendance (ADX)
        if adx > 30:
            score += 0.1
        elif adx > 20:
            score += 0.05

        return max(0.0, min(1.0, score))

    def _get_tf_data(self, connector, symbol: str, timeframe: str) -> Optional[Dict]:
        """Recupere les donnees OHLC pour un timeframe specifique."""
        if hasattr(connector, '_get_tf_ohlc'):
            return connector._get_tf_ohlc(symbol, timeframe, self.bars_per_tf)
        original_tf = connector.timeframe_str
        original_seconds = connector.timeframe_seconds
        try:
            connector.timeframe_str = timeframe
            connector.timeframe_seconds = TF_SECONDS.get(timeframe, 900)
            ohlc = connector.get_ohlc_data(symbol)
            return ohlc
        except Exception as e:
            logger.debug(f"[Multi-TF] Erreur donnees {timeframe} pour {symbol}: {e}")
            return None
        finally:
            connector.timeframe_str = original_tf
            connector.timeframe_seconds = original_seconds

    def _make_tf_config(self, timeframe: str) -> dict:
        """Cree une config adaptee pour un timeframe superieur."""
        cfg = copy.deepcopy(self.config)
        mult = TF_INDICATOR_MULT.get(timeframe, 1.0)
        ind = cfg["indicators"]
        base_fast = ind["ema_fast"]
        ind["ema_fast"] = max(3, int(base_fast * mult))
        ind["ema_medium"] = max(5, int(ind["ema_medium"] * mult))
        ind["ema_slow"] = max(10, int(ind["ema_slow"] * mult))
        return cfg

    def _disabled_result(self, base_signal: str) -> Dict:
        return {
            "confirmed": True, "signal": base_signal,
            "confluence_score": 1.0, "lot_multiplier": 1.0,
            "timeframe_results": {},
            "details": "Multi-TF desactive",
            "structure_score": 0.0, "momentum_score": 0.0,
            "confluence_quality": "bonne",
            "trend_alignment": 0.5,
            "rsi_divergence": {"detected": False, "type": None, "strength": 0.0, "penalty": 0.0, "count": 0},
            "macd_divergence": {"detected": False, "type": None, "strength": 0.0, "penalty": 0.0, "count": 0},
            "volume_confirmation": 0.5,
            "sr_levels": [],
        }

    def get_status_report(self) -> str:
        """Rapport de statut du module multi-TF."""
        lines = [
            f"Multi-Timeframe v3: {'ACTIVE' if self.enabled else 'DESACTIVE'}",
            f"  TF de base: {self.base_tf}",
            f"  TFs de confirmation: {', '.join(self.confirm_tfs)}",
            f"  Mode: {self.mode}",
            f"  Accord minimum: {self.min_agreement}",
            f"  Analyse structurelle (HH/HL/LH/LL): {'ACTIVEE' if self.use_structure else 'DESACTIVEE'}",
            f"  Analyse momentum (RSI+MACD): {'ACTIVEE' if self.use_momentum else 'DESACTIVEE'}",
            f"  Detection divergence RSI: {'ACTIVEE' if self.use_divergence else 'DESACTIVEE'}",
            f"  Detection divergence MACD: {'ACTIVEE' if self.use_macd_divergence else 'DESACTIVEE'}",
            f"  Confirmation volume: {'ACTIVEE' if self.use_volume else 'DESACTIVEE'}",
            f"  Niveaux S/R croises: {'ACTIFS' if self.use_sr_levels else 'DESACTIVES'}",
            f"  Multiplicateur lot adaptatif: {'ACTIVE' if self.lot_multiplier else 'DESACTIVE'}",
        ]
        return "\n".join(lines)
