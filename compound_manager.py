r"""
Gestionnaire de croissance composée avancé.
Gère automatiquement la taille des positions pour accroître
le capital progressivement à partir d'un petit montant (5$).

Stratégie :
  - Commence avec la mise minimale (ex: 0.35$ sur Deriv)
  - Après chaque gain, augmente progressivement la mise
  - Après une perte, réduit la mise pour protéger le capital
  - Objectif : faire croître 5$ -> 10$ -> 25$ -> 50$ -> 100$+...

Améliorations par rapport à la version initiale :
  - 24 paliers granulaires de 5$ à 200 000$+
  - Suivi d'état par broker (fichiers séparés)
  - Historique des 50 derniers trades avec résultats
  - Rétrogradation automatique après X pertes consécutives au palier actuel
  - Filet de sécurité : baisse de 50% depuis le pic = -2 paliers
  - Notifications de jalons
  - Écriture atomique de l'état (fichier temporaire + renommage)
  - Statistiques de session (meilleur/pire trade, moyennes, ratio de Sharpe)
  - Recommandations de trading basées sur la performance
"""

import logging
import json
import os
import math
import tempfile
from datetime import datetime, date
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


# ==================================================================
#  PALIERS DE CROISSANCE : 24 niveaux de 5$ à 200 000$+
# ==================================================================
GROWTH_LEVELS = [
    {"balance": 5,       "base_stake": 0.35,  "label": "Niveau 1  - Départ 5$"},
    {"balance": 8,       "base_stake": 0.50,  "label": "Niveau 2  - 8$"},
    {"balance": 12,      "base_stake": 0.70,  "label": "Niveau 3  - 12$"},
    {"balance": 18,      "base_stake": 1.00,  "label": "Niveau 4  - 18$"},
    {"balance": 25,      "base_stake": 1.50,  "label": "Niveau 5  - 25$"},
    {"balance": 40,      "base_stake": 2.00,  "label": "Niveau 6  - 40$"},
    {"balance": 60,      "base_stake": 3.00,  "label": "Niveau 7  - 60$"},
    {"balance": 100,     "base_stake": 5.00,  "label": "Niveau 8  - 100$"},
    {"balance": 150,     "base_stake": 7.00,  "label": "Niveau 9  - 150$"},
    {"balance": 250,     "base_stake": 10.00, "label": "Niveau 10 - 250$"},
    {"balance": 400,     "base_stake": 15.00, "label": "Niveau 11 - 400$"},
    {"balance": 600,     "base_stake": 20.00, "label": "Niveau 12 - 600$"},
    {"balance": 1000,    "base_stake": 30.00, "label": "Niveau 13 - 1 000$"},
    {"balance": 1500,    "base_stake": 45.00, "label": "Niveau 14 - 1 500$"},
    {"balance": 2500,    "base_stake": 70.00, "label": "Niveau 15 - 2 500$"},
    {"balance": 4000,    "base_stake": 100.00, "label": "Niveau 16 - 4 000$"},
    {"balance": 7000,    "base_stake": 150.00, "label": "Niveau 17 - 7 000$"},
    {"balance": 10000,   "base_stake": 220.00, "label": "Niveau 18 - 10 000$"},
    {"balance": 15000,   "base_stake": 320.00, "label": "Niveau 19 - 15 000$"},
    {"balance": 25000,   "base_stake": 500.00, "label": "Niveau 20 - 25 000$"},
    {"balance": 40000,   "base_stake": 800.00, "label": "Niveau 21 - 40 000$"},
    {"balance": 70000,   "base_stake": 1200.00, "label": "Niveau 22 - 70 000$"},
    {"balance": 100000,  "base_stake": 1800.00, "label": "Niveau 23 - 100 000$"},
    {"balance": 200000,  "base_stake": 3000.00, "label": "Niveau 24 - 200 000$"},
]

# ==================================================================
#  JALONS DE NOTIFICATION
# ==================================================================
MILESTONE_BALANCES = [
    10, 25, 50, 100, 250, 500, 1000, 2500, 5000,
    10000, 25000, 50000, 100000, 200000,
]


class CompoundManager:
    r"""
    Gestionnaire de croissance composée avancée du capital.
    Adapte automatiquement la mise en fonction du solde,
    avec rétrogradation, filet de sécurité et recommandations.
    """

    def __init__(self, config: dict):
        self.config = config
        compound_cfg = config.get("compound_growth", {})
        self.enabled = compound_cfg.get("enabled", True)
        self.growth_levels = compound_cfg.get("custom_levels", GROWTH_LEVELS)
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.win_boost = compound_cfg.get("win_boost_pct", 20)
        self.loss_reduction = compound_cfg.get("loss_reduction_pct", 30)
        self.max_boost_streak = compound_cfg.get("max_boost_streak", 3)
        self.max_reduction_streak = compound_cfg.get("max_reduction_streak", 2)
        self.state_file = compound_cfg.get("state_file", "compound_state.json")
        self.total_trades = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_pnl = 0.0
        self.starting_balance = 0.0
        self.peak_balance = 0.0

        # --- NOUVEAUX PARAMÈTRES ---

        # Broker courant pour suivi d'état séparé
        self.broker_name = compound_cfg.get("broker_name", "default")

        # Rétrogradation automatique après X pertes consécutives au palier actuel
        self.downgrade_after_losses = compound_cfg.get("downgrade_after_losses", 5)

        # Filet de sécurité : seuil de baisse depuis le pic pour déclasser de 2 niveaux
        self.safety_net_drop_pct = compound_cfg.get("safety_net_drop_pct", 50.0)

        # Historique des performances (derniers 50 trades)
        self.performance_history: List[Dict] = []
        self.max_history_size = compound_cfg.get("max_history_size", 50)

        # Jalons déjà atteints (pour éviter les doublons de notification)
        self.reached_milestones: List[float] = []
        self.milestones = compound_cfg.get("milestones", MILESTONE_BALANCES)

        # Rétrogradation manuelle (niveau forcé)
        self.forced_level_offset = 0  # Négatif = niveaux en dessous

        # Statistiques de session
        self.session_start_time: Optional[datetime] = None
        self.session_best_trade = 0.0
        self.session_worst_trade = 0.0
        self.session_total_wins_pnl = 0.0
        self.session_total_losses_pnl = 0.0
        self.session_win_count = 0
        self.session_loss_count = 0

        self._load_state()

    # ==================================================================
    #  GESTION DE L'ÉTAT PAR BROKER
    # ==================================================================

    def _get_broker_state_file(self) -> str:
        """
        Retourne le chemin du fichier d'état spécifique au broker.

        Returns:
            Chemin du fichier d'état JSON pour le broker courant
        """
        base, ext = os.path.splitext(self.state_file)
        return f"{base}_{self.broker_name}{ext}"

    def _load_state(self):
        """Charge l'état précédent depuis un fichier avec support multi-broker."""
        state_path = self._get_broker_state_file()
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.consecutive_wins = state.get("consecutive_wins", 0)
                self.consecutive_losses = state.get("consecutive_losses", 0)
                self.total_trades = state.get("total_trades", 0)
                self.total_wins = state.get("total_wins", 0)
                self.total_losses = state.get("total_losses", 0)
                self.total_pnl = state.get("total_pnl", 0.0)
                self.peak_balance = state.get("peak_balance", 0.0)
                self.starting_balance = state.get("starting_balance", 0.0)
                self.forced_level_offset = state.get("forced_level_offset", 0)
                self.reached_milestones = state.get("reached_milestones", [])
                # Chargement de l'historique de performance
                self.performance_history = state.get("performance_history", [])
                # Statistiques de session
                session = state.get("session", {})
                self.session_best_trade = session.get("best_trade", 0.0)
                self.session_worst_trade = session.get("worst_trade", 0.0)
                self.session_total_wins_pnl = session.get("total_wins_pnl", 0.0)
                self.session_total_losses_pnl = session.get("total_losses_pnl", 0.0)
                self.session_win_count = session.get("win_count", 0)
                self.session_loss_count = session.get("loss_count", 0)
                logger.info(
                    f"état composé chargé [{self.broker_name}] : "
                    f"{self.total_trades} trades, PnL total={self.total_pnl:.2f}$, "
                    f"pic={self.peak_balance:.2f}$, historique={len(self.performance_history)} trades"
                )
            except Exception as e:
                logger.warning(f"impossible de charger l'état [{self.broker_name}] : {e}")

    def _save_state(self):
        """
        Sauvegarde l'état dans un fichier de manière atomique.
        Écrit d'abord dans un fichier temporaire, puis renomme
        pour éviter la corruption en cas de crash.
        """
        state = {
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "total_trades": self.total_trades,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_pnl": self.total_pnl,
            "peak_balance": self.peak_balance,
            "starting_balance": self.starting_balance,
            "forced_level_offset": self.forced_level_offset,
            "reached_milestones": self.reached_milestones,
            "performance_history": self.performance_history[-self.max_history_size:],
            "session": {
                "best_trade": self.session_best_trade,
                "worst_trade": self.session_worst_trade,
                "total_wins_pnl": self.session_total_wins_pnl,
                "total_losses_pnl": self.session_total_losses_pnl,
                "win_count": self.session_win_count,
                "loss_count": self.session_loss_count,
            },
            "updated": datetime.now().isoformat(),
        }
        state_path = self._get_broker_state_file()
        try:
            # Écriture atomique : fichier temporaire puis renommage
            state_dir = os.path.dirname(state_path) or "."
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix="compound_", dir=state_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, state_path)
                logger.debug(
                    f"état sauvegardé avec succès [{self.broker_name}] : {state_path}"
                )
            except Exception:
                # Nettoyer le fichier temporaire en cas d'erreur
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.warning(
                f"impossible de sauvegarder l'état [{self.broker_name}] : {e}"
            )

    # ==================================================================
    #  INITIALISATION
    # ==================================================================

    def initialize(self, balance: float):
        """
        Initialise avec le solde de départ.

        Args:
            balance: Solde actuel du compte
        """
        if self.starting_balance == 0:
            self.starting_balance = balance
        self.peak_balance = max(self.peak_balance, balance)
        self.session_start_time = datetime.now()
        logger.info(
            f"initialisation composé [{self.broker_name}] : "
            f"solde={balance:.2f}$, pic={self.peak_balance:.2f}$"
        )

    # ==================================================================
    #  NIVEAU ACTUEL
    # ==================================================================

    def get_current_level(self, balance: float) -> Dict:
        """
        Retourne le palier de croissance actuel.
        Tient compte de l'offset de rétrogradation éventuel.

        Args:
            balance: Solde actuel du compte

        Returns:
            Dictionnaire avec 'current', 'next', 'progress_pct', 'level_index'
        """
        # Appliquer l'offset de rétrogradation
        adjusted_levels = self._get_adjusted_levels()

        current_level = adjusted_levels[0]
        current_index = 0
        next_level = None

        for i, level in enumerate(adjusted_levels):
            if balance >= level["balance"]:
                current_level = level
                current_index = i
                if i + 1 < len(adjusted_levels):
                    next_level = adjusted_levels[i + 1]
            else:
                break

        return {
            "current": current_level,
            "next": next_level,
            "progress_pct": self._progress_to_next(balance, current_level, next_level),
            "level_index": current_index,
            "forced_offset": self.forced_level_offset,
        }

    def _get_adjusted_levels(self) -> List[Dict]:
        """
        Retourne la liste des niveaux ajustée par l'offset de rétrogradation.
        Un offset négatif décale le niveau affiché vers le bas.

        Returns:
            Liste des niveaux ajustés
        """
        if self.forced_level_offset >= 0:
            return self.growth_levels
        # Rétrogradation : on considère le niveau comme étant plus bas
        offset = abs(self.forced_level_offset)
        if offset >= len(self.growth_levels):
            offset = len(self.growth_levels) - 1
        # Retourner les niveaux à partir de l'index décalé
        return self.growth_levels[offset:]

    def _progress_to_next(self, balance: float, current: Dict, next_level: Optional[Dict]) -> float:
        """Calcule le % de progression vers le prochain palier."""
        if next_level is None:
            return 100.0
        current_bal = current["balance"]
        next_bal = next_level["balance"]
        if next_bal <= current_bal:
            return 100.0
        progress = ((balance - current_bal) / (next_bal - current_bal)) * 100
        return round(min(100, max(0, progress)), 1)

    # ==================================================================
    #  CALCUL DE LA MISE
    # ==================================================================

    def calculate_stake(self, balance: float, confidence: float = 50.0) -> float:
        """
        Calcule la mise optimale basée sur le solde et la confiance.

        Args:
            balance: Solde actuel du compte
            confidence: Confiance du signal (0-100)

        Returns:
            Mise recommandée
        """
        if not self.enabled:
            return self.config["trading"]["default_lot_size"]

        level_info = self.get_current_level(balance)
        base_stake = level_info["current"]["base_stake"]

        # Ajustement selon les séries de gains/pertes
        multiplier = 1.0

        # Boost après gains consécutifs
        if self.consecutive_wins > 0:
            boost = min(self.consecutive_wins, self.max_boost_streak)
            multiplier += (boost * self.win_boost / 100)
            logger.debug(
                f"boost gain : x{multiplier:.2f} ({self.consecutive_wins} gains consécutifs)"
            )

        # Réduction après pertes consécutives
        if self.consecutive_losses > 0:
            reduction = min(self.consecutive_losses, self.max_reduction_streak)
            multiplier -= (reduction * self.loss_reduction / 100)
            multiplier = max(0.3, multiplier)  # jamais en dessous de 30%
            logger.debug(
                f"réduction perte : x{multiplier:.2f} ({self.consecutive_losses} pertes consécutives)"
            )

        # Ajustement selon la confiance du signal
        if confidence >= 80:
            multiplier *= 1.1  # +10% pour signal très confiant
        elif confidence < 40:
            multiplier *= 0.7  # -30% pour signal peu confiant

        stake = base_stake * multiplier

        # Limiter pour ne pas risquer plus de 10% du solde par trade
        max_stake = balance * 0.10
        stake = min(stake, max_stake)

        # Arrondir
        lot_step = self.config["trading"].get("lot_step", 0.01)
        stake = round(stake / lot_step) * lot_step
        stake = max(self.config["trading"]["min_lot_size"], stake)

        logger.info(
            f"mise calculée [{self.broker_name}] : {stake:.2f}$ "
            f"(base={base_stake:.2f}$ x {multiplier:.2f}, conf={confidence:.0f}%)"
        )
        return stake

    # ==================================================================
    #  ENREGISTREMENT DU RÉSULTAT D'UN TRADE
    # ==================================================================

    def record_trade_result(self, pnl: float, balance: float = 0.0):
        """
        Enregistre le résultat d'un trade et ajuste les compteurs.
        Gère la rétrogradation automatique, le filet de sécurité,
        et les notifications de jalons.

        Args:
            pnl: Résultat du trade (positif = gain, négatif = perte)
            balance: Solde actuel (optionnel, pour le filet de sécurité)
        """
        self.total_trades += 1
        self.total_pnl += pnl

        # Mise à jour des statistiques de session
        if pnl > 0:
            self.total_wins += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self.session_win_count += 1
            self.session_total_wins_pnl += pnl
            self.session_best_trade = max(self.session_best_trade, pnl)
            logger.info(
                f"GAIN +{pnl:.2f}$ [{self.broker_name}] | "
                f"série gagnante : {self.consecutive_wins}"
            )
        else:
            self.total_losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self.session_loss_count += 1
            self.session_total_losses_pnl += pnl
            self.session_worst_trade = min(self.session_worst_trade, pnl)
            logger.info(
                f"PERTE {pnl:.2f}$ [{self.broker_name}] | "
                f"série perdante : {self.consecutive_losses}"
            )

        # Enregistrement dans l'historique de performance
        self._add_to_history(pnl)

        # Rétrogradation automatique après X pertes consécutives
        if self.consecutive_losses >= self.downgrade_after_losses:
            self._apply_downgrade()

        # Filet de sécurité : baisse de 50% depuis le pic = -2 niveaux
        if balance > 0 and self.peak_balance > 0:
            drop_pct = (self.peak_balance - balance) / self.peak_balance * 100
            if drop_pct >= self.safety_net_drop_pct:
                self._apply_safety_net(balance)

        # Vérification des jalons
        if balance > 0:
            self._check_milestones(balance)

        self._save_state()

    # ==================================================================
    #  MISE À JOUR DU PIC
    # ==================================================================

    def update_peak(self, balance: float):
        """Met à jour le pic de balance."""
        if balance > self.peak_balance:
            self.peak_balance = balance
            # Remise à zéro de l'offset de rétrogradation quand on atteint un nouveau pic
            if self.forced_level_offset < 0:
                logger.info(
                    f"nouveau pic atteint ({balance:.2f}$) : "
                    f"réinitialisation de la rétrogradation"
                )
                self.forced_level_offset = 0
            self._save_state()

    # ==================================================================
    #  STATISTIQUES
    # ==================================================================

    def get_stats(self) -> Dict:
        """
        Retourne les statistiques de croissance complètes
        incluant les métriques de session et de performance.

        Returns:
            Dictionnaire avec toutes les statistiques
        """
        win_rate = (self.total_wins / self.total_trades * 100) if self.total_trades > 0 else 0

        # Statistiques de session
        avg_win = (
            self.session_total_wins_pnl / self.session_win_count
            if self.session_win_count > 0 else 0.0
        )
        avg_loss = (
            self.session_total_losses_pnl / self.session_loss_count
            if self.session_loss_count > 0 else 0.0
        )

        # Ratio de Sharpe (simplifié) basé sur l'historique
        sharpe = self._calculate_sharpe_ratio()

        return {
            # Statistiques globales
            "starting_balance": self.starting_balance,
            "peak_balance": self.peak_balance,
            "total_pnl": self.total_pnl,
            "total_trades": self.total_trades,
            "wins": self.total_wins,
            "losses": self.total_losses,
            "win_rate": round(win_rate, 1),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "growth_pct": round(
                ((self.peak_balance - self.starting_balance) / self.starting_balance * 100)
                if self.starting_balance > 0 else 0, 1
            ),
            # Statistiques de session
            "session_best_trade": self.session_best_trade,
            "session_worst_trade": self.session_worst_trade,
            "session_avg_win": round(avg_win, 2),
            "session_avg_loss": round(avg_loss, 2),
            "session_win_count": self.session_win_count,
            "session_loss_count": self.session_loss_count,
            # Performance avancée
            "sharpe_ratio": round(sharpe, 2),
            "history_size": len(self.performance_history),
            "forced_level_offset": self.forced_level_offset,
            "broker": self.broker_name,
            # Jalons
            "milestones_reached": len(self.reached_milestones),
            "total_milestones": len(self.milestones),
        }

    # ==================================================================
    #  MESSAGE DE PROGRESSION
    # ==================================================================

    def get_progress_message(self, balance: float) -> str:
        """
        Retourne un message de progression motivant.

        Args:
            balance: Solde actuel

        Returns:
            Chaîne descriptive de la progression
        """
        level_info = self.get_current_level(balance)
        current = level_info["current"]
        next_lvl = level_info["next"]
        progress = level_info["progress_pct"]

        # Indicateur de rétrogradation
        retro_info = ""
        if self.forced_level_offset < 0:
            retro_info = f" [RÉTROGRADÉ de {abs(self.forced_level_offset)} niveaux]"

        msg = f"{current['label']} | Mise: {current['base_stake']}${retro_info}"
        if next_lvl:
            remaining = next_lvl["balance"] - balance
            msg += (
                f" | Prochain: {next_lvl['balance']}$ "
                f"(reste {remaining:.1f}$, {progress:.0f}%)"
            )
        else:
            msg += " | PALIER MAX ATTEINT !"

        # Ajouter le nombre de jalons atteints
        if self.reached_milestones:
            msg += f" | Jalons: {len(self.reached_milestones)}/{len(self.milestones)}"

        return msg

    # ==================================================================
    #  RECOMMANDATION
    # ==================================================================

    def get_recommendation(self, balance: float) -> Dict:
        """
        Retourne une recommandation de trading basée sur la performance actuelle.

        Args:
            balance: Solde actuel du compte

        Returns:
            Dictionnaire avec 'action', 'reason', 'suggested_stake', 'urgency'
        """
        actions = []
        urgency = "normale"  # normale, elevee, critique
        suggested_stake = self.calculate_stake(balance)

        # Analyser les séries
        if self.consecutive_losses >= self.downgrade_after_losses:
            actions.append(
                f"série perdante critique ({self.consecutive_losses} pertes) : "
                f"pause recommandée et rétrogradation appliquée"
            )
            urgency = "critique"
        elif self.consecutive_losses >= self.max_reduction_streak:
            actions.append(
                f"série perdante en cours ({self.consecutive_losses} pertes) : "
                f"réduire la mise et attendre un signal de confirmation"
            )
            urgency = "elevee"

        if self.consecutive_wins >= self.max_boost_streak:
            actions.append(
                f"excellente série gagnante ({self.consecutive_wins} gains) : "
                f"poursuivre avec prudence, ne pas augmenter brutalement"
            )

        # Analyser le win rate récent
        recent_wr = self._get_recent_win_rate()
        if recent_wr < 0.35 and self.total_trades >= 10:
            actions.append(
                f"win rate récent très faible ({recent_wr:.0%}) : "
                f"revoir la stratégie ou faire une pause"
            )
            urgency = "elevee"
        elif recent_wr > 0.7 and len(self.performance_history) >= 10:
            actions.append(
                f"win rate récent excellent ({recent_wr:.0%}) : "
                f"conditions favorables pour le trading"
            )

        # Analyser le drawdown
        if balance > 0 and self.peak_balance > 0:
            drop_pct = (self.peak_balance - balance) / self.peak_balance * 100
            if drop_pct >= 40:
                actions.append(
                    f"drawdown sévère ({drop_pct:.1f}% depuis le pic) : "
                    f"filet de sécurité proche, réduire drastiquement l'exposition"
                )
                urgency = "critique"
            elif drop_pct >= 20:
                actions.append(
                    f"drawdown notable ({drop_pct:.1f}% depuis le pic) : "
                    f"prudence recommandée"
                )
                if urgency == "normale":
                    urgency = "elevee"

        # Analyser le ratio de Sharpe
        sharpe = self._calculate_sharpe_ratio()
        if sharpe < -0.5 and len(self.performance_history) >= 10:
            actions.append(
                f"ratio de Sharpe négatif ({sharpe:.2f}) : "
                f"le risque dépasse les rendements, ajuster la stratégie"
            )
            if urgency == "normale":
                urgency = "elevee"

        # Analyser la progression
        level_info = self.get_current_level(balance)
        progress = level_info["progress_pct"]
        if progress >= 80 and level_info["next"] is not None:
            actions.append(
                f"proche du prochain palier ({progress:.0f}%) : "
                f"maintenir le rythme actuel"
            )

        # Si aucune action spécifique
        if not actions:
            actions.append("conditions normales : poursuivre le plan de trading actuel")

        return {
            "action": " | ".join(actions),
            "suggested_stake": suggested_stake,
            "urgency": urgency,
            "recent_win_rate": round(recent_wr * 100, 1),
            "sharpe_ratio": round(sharpe, 2),
            "current_level": level_info["current"]["label"],
            "broker": self.broker_name,
        }

    # ==================================================================
    #  NOTIFICATIONS DE JALONS
    # ==================================================================

    def get_pending_milestones(self, balance: float) -> List[str]:
        """
        Vérifie et retourne les nouveaux jalons atteints depuis le dernier appel.

        Args:
            balance: Solde actuel

        Returns:
            Liste des messages de notification de jalons
        """
        notifications = []
        for milestone in self.milestones:
            if balance >= milestone and milestone not in self.reached_milestones:
                self.reached_milestones.append(milestone)
                growth = (
                    ((milestone - self.starting_balance) / self.starting_balance * 100)
                    if self.starting_balance > 0 else 0
                )
                notifications.append(
                    f"🏆 JALON ATTEINT [{self.broker_name}] : {milestone}$ ! "
                    f"Croissance : +{growth:.0f}% depuis le début "
                    f"({self.starting_balance}$ -> {milestone}$)"
                )
        if notifications:
            self._save_state()
        return notifications

    # ==================================================================
    #  MÉTHODES PRIVÉES
    # ==================================================================

    def _add_to_history(self, pnl: float):
        """
        Ajoute un résultat de trade à l'historique de performance.

        Args:
            pnl: Résultat du trade
        """
        entry = {
            "pnl": round(pnl, 2),
            "timestamp": datetime.now().isoformat(),
            "balance_after": 0.0,  # Sera mis à jour si le solde est fourni
        }
        self.performance_history.append(entry)
        # Garder seulement les N derniers trades
        if len(self.performance_history) > self.max_history_size:
            self.performance_history = self.performance_history[-self.max_history_size:]
        logger.debug(
            f"historique mis à jour [{self.broker_name}] : "
            f"{len(self.performance_history)}/{self.max_history_size} trades"
        )

    def _get_recent_win_rate(self) -> float:
        """
        Calcule le win rate sur les derniers trades de l'historique.

        Returns:
            Win rate entre 0.0 et 1.0, 0.5 si pas assez de données
        """
        if len(self.performance_history) < 5:
            return 0.5
        wins = sum(1 for t in self.performance_history if t["pnl"] > 0)
        return wins / len(self.performance_history)

    def _calculate_sharpe_ratio(self) -> float:
        """
        Calcule un ratio de Sharpe simplifié basé sur l'historique.
        Utilise un taux sans risque de 0% pour simplification.

        Returns:
            Ratio de Sharpe (annuelisé approximativement)
        """
        if len(self.performance_history) < 5:
            return 0.0

        pnls = [t["pnl"] for t in self.performance_history]
        n = len(pnls)
        mean_pnl = sum(pnls) / n

        if mean_pnl == 0:
            return 0.0

        # Variance
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / n
        std_pnl = math.sqrt(variance)

        if std_pnl == 0:
            return 0.0

        # Ratio de Sharpe annuelisé (hypothèse ~252 trades/an)
        sharpe = (mean_pnl / std_pnl) * math.sqrt(252)
        return sharpe

    def _apply_downgrade(self):
        """
        Applique une rétrogradation d'un niveau après trop de pertes consécutives.
        """
        self.forced_level_offset -= 1
        # Limiter la rétrogradation à ne pas descendre en dessous du niveau 1
        max_down = -(len(self.growth_levels) - 1)
        self.forced_level_offset = max(max_down, self.forced_level_offset)
        logger.warning(
            f"RÉTROGRADATION [{self.broker_name}] : {self.consecutive_losses} pertes consécutives, "
            f"niveau ajusté de {self.forced_level_offset + 1} (offset={self.forced_level_offset})"
        )

    def _apply_safety_net(self, balance: float):
        """
        Applique le filet de sécurité : baisse de 2 niveaux si le solde
        a chuté de plus de 50% depuis le pic.

        Args:
            balance: Solde actuel
        """
        old_offset = self.forced_level_offset
        self.forced_level_offset -= 2
        # Limiter
        max_down = -(len(self.growth_levels) - 1)
        self.forced_level_offset = max(max_down, self.forced_level_offset)
        logger.warning(
            f"FILET DE SÉCURITÉ [{self.broker_name}] : "
            f"solde={balance:.2f}$ (pic={self.peak_balance:.2f}$), "
            f"rétrogradation de 2 niveaux (offset {old_offset} -> {self.forced_level_offset})"
        )

    def _check_milestones(self, balance: float):
        """
        Vérifie les jalons atteints et les enregistre.

        Args:
            balance: Solde actuel
        """
        for milestone in self.milestones:
            if balance >= milestone and milestone not in self.reached_milestones:
                self.reached_milestones.append(milestone)
                growth = (
                    ((milestone - self.starting_balance) / self.starting_balance * 100)
                    if self.starting_balance > 0 else 0
                )
                logger.info(
                    f"🏆 JALON [{self.broker_name}] : {milestone}$ atteint ! "
                    f"Croissance : +{growth:.0f}%"
                )

    def reset_session_stats(self):
        """Réinitialise les statistiques de session."""
        self.session_best_trade = 0.0
        self.session_worst_trade = 0.0
        self.session_total_wins_pnl = 0.0
        self.session_total_losses_pnl = 0.0
        self.session_win_count = 0
        self.session_loss_count = 0
        self.session_start_time = datetime.now()
        logger.info(f"statistiques de session réinitialisées [{self.broker_name}]")

    def set_broker(self, broker_name: str):
        """
        Change le broker actuel et recharge l'état correspondant.

        Args:
            broker_name: Nom du broker
        """
        # Sauvegarder l'état actuel avant de changer
        self._save_state()
        self.broker_name = broker_name
        # Réinitialiser et recharger
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.forced_level_offset = 0
        self.performance_history = []
        self.reached_milestones = []
        self._load_state()
        logger.info(f"broker changé vers [{broker_name}]")
