r"""
Gestionnaire de risque avancé pour le robot de trading MetaTrader 5.
Gère le dimensionnement des positions, stop-loss, take-profit,
trailing stop (classique et ATR), breakeven, les limites quotidiennes,
la surveillance de la courbe d'équité, le mode récupération,
la réduction de risque liée aux actualités, et le score de risque global.
"""

import logging
from datetime import datetime, date, time
from typing import Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Gestionnaire de risque complet et avancé.
    Protège le capital avec des règles strictes et un score de risque dynamique.
    """

    def __init__(self, config: dict, account_info: dict):
        self.config = config
        risk_cfg = config["risk_management"]
        sl_tp_cfg = config["stop_loss_take_profit"]
        trading_cfg = config["trading"]

        # Paramètres de risque de base
        self.max_risk_pct = risk_cfg["max_risk_per_trade_pct"]
        self.max_daily_loss_pct = risk_cfg["max_daily_loss_pct"]
        self.max_daily_trades = risk_cfg["max_daily_trades"]
        self.use_trailing_stop = risk_cfg["use_trailing_stop"]
        self.trailing_stop_pips = risk_cfg["trailing_stop_pips"]
        self.trailing_step_pips = risk_cfg["trailing_step_pips"]
        self.break_even_after_pips = risk_cfg["break_even_after_pips"]
        self.move_sl_to_be_pips = risk_cfg["move_sl_to_be_pips"]

        # Paramètres SL/TP
        self.default_sl_pips = sl_tp_cfg["default_stop_loss_pips"]
        self.default_tp_pips = sl_tp_cfg["default_take_profit_pips"]
        self.risk_reward_ratio = sl_tp_cfg["risk_reward_ratio"]
        self.use_dynamic_sl_tp = sl_tp_cfg["use_dynamic_sl_tp"]
        self.atr_sl_mult = sl_tp_cfg["atr_sl_multiplier"]
        self.atr_tp_mult = sl_tp_cfg["atr_tp_multiplier"]

        # Trading
        self.max_positions = trading_cfg["max_open_positions"]
        self.default_lot = trading_cfg["default_lot_size"]
        self.max_lot = trading_cfg["max_lot_size"]
        self.min_lot = trading_cfg["min_lot_size"]
        self.lot_step = trading_cfg["lot_step"]

        # --- NOUVEAUX PARAMÈTRES AVANCÉS ---

        # Pertes consécutives avant pause forcée (défaut : 5)
        self.max_consecutive_losses = risk_cfg.get("max_consecutive_losses", 5)

        # Drawdown maximum par session en % (défaut : 10%)
        self.max_session_drawdown_pct = risk_cfg.get("max_session_drawdown_pct", 10.0)

        # Seuil d'alerte de la courbe d'équité en % depuis le pic (défaut : 8%)
        self.equity_curve_alert_pct = risk_cfg.get("equity_curve_alert_pct", 8.0)

        # Limites de risque par symbole par jour (défaut : {} = pas de limite)
        self.symbol_daily_loss_limits: Dict[str, float] = risk_cfg.get(
            "symbol_daily_loss_limits", {}
        )
        # Limite par symbole par défaut si non spécifiée (% du solde)
        self.default_symbol_daily_loss_pct = risk_cfg.get(
            "default_symbol_daily_loss_pct", 3.0
        )

        # Mode récupération : activer après une perte > X % du solde (défaut : 5%)
        self.recovery_mode_threshold_pct = risk_cfg.get("recovery_mode_threshold_pct", 5.0)
        # Facteur de réduction en mode récupération (défaut : 0.5 = 50%)
        self.recovery_size_factor = risk_cfg.get("recovery_size_factor", 0.5)
        # Nombre de trades de confirmation requis avant de sortir du mode récupération
        self.recovery_confirmation_trades = risk_cfg.get("recovery_confirmation_trades", 2)

        # Réduction de risque pendant les heures d'actualités à fort impact
        self.news_risk_reduction_enabled = risk_cfg.get("news_risk_reduction_enabled", False)
        # Facteur de réduction pendant les actualités (défaut : 0.5)
        self.news_risk_factor = risk_cfg.get("news_risk_factor", 0.5)
        # Plages horaires d'actualités à fort impact (liste de tuples (heure_début, heure_fin))
        self.news_hours: List[Tuple[int, int]] = risk_cfg.get(
            "news_hours", [
                (8, 10),   # Ouverture Londres / données économiques
                (12, 15),  # Ouverture New York / annonces FED
            ]
        )

        # Limite d'exposition totale (valeur notionnelle max en $)
        self.max_total_exposure = risk_cfg.get("max_total_exposure", 0)  # 0 = désactivé

        # Nombre max de positions par symbole (défaut : 2)
        self.max_positions_per_symbol = risk_cfg.get("max_positions_per_symbol", 2)

        # Trailing stop basé sur l'ATR
        self.use_atr_trailing = risk_cfg.get("use_atr_trailing_stop", False)
        self.atr_trailing_mult = risk_cfg.get("atr_trailing_multiplier", 2.0)

        # Fermeture partielle : pourcentage de la position à fermer au trailing (défaut : 0 = désactivé)
        self.partial_close_pct = risk_cfg.get("partial_close_pct", 0.0)
        # Seuil en pips pour déclencher la fermeture partielle
        self.partial_close_pips = risk_cfg.get("partial_close_pips", 20.0)

        # --- ÉTAT DU COMPTE ---
        self.balance = float(account_info.get("balance", 10000))
        self.equity = float(account_info.get("equity", 10000))
        self.currency = account_info.get("currency", "USD")

        # --- COMPTEURS QUOTIDIENS ---
        self.daily_pnl = 0.0
        self.daily_trade_count = 0
        self.last_reset_date: Optional[date] = None
        self.daily_symbol_pnl: Dict[str, float] = {}
        self.daily_symbol_trades: Dict[str, int] = {}
        self.daily_positions_per_symbol: Dict[str, int] = {}

        # --- COMPTEURS DE PERTES CONSÉCUTIVES ---
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.paused_until: Optional[datetime] = None
        self.pause_reason = ""

        # --- SURVEILLANCE DE LA COURBE D'ÉQUITÉ ---
        self.peak_equity = self.equity
        self.equity_alert_triggered = False
        self.equity_alert_time: Optional[datetime] = None

        # --- DRAWDOWN DE SESSION ---
        self.session_start_balance = self.balance
        self.session_peak_balance = self.balance

        # --- MODE RÉCUPÉRATION ---
        self.recovery_mode = False
        self.recovery_confirmation_count = 0
        self.recovery_trigger_time: Optional[datetime] = None
        self.last_big_loss_amount = 0.0

        # --- HISTORIQUE RÉCENT POUR WIN RATE ---
        self.recent_trades: List[float] = []  # Derniers résultats de trades (PnL)
        self.win_rate_window = risk_cfg.get("win_rate_window", 20)  # Fenêtre pour calculer le WR

        # --- EXPOSITION TOTALE ---
        self.current_total_exposure = 0.0

        self._reset_daily_if_needed()

    # ==================================================================
    #  RÉINITIALISATION QUOTIDIENNE
    # ==================================================================

    def _reset_daily_if_needed(self):
        """Remet à zéro les compteurs quotidiens si on change de jour."""
        today = date.today()
        if self.last_reset_date != today:
            self.daily_pnl = 0.0
            self.daily_trade_count = 0
            self.daily_symbol_pnl = {}
            self.daily_symbol_trades = {}
            self.daily_positions_per_symbol = {}
            self.equity_alert_triggered = False
            self.equity_alert_time = None
            self.last_reset_date = today
            logger.info(
                f"réinitialisation quotidienne des compteurs pour le {today}"
            )

    # ==================================================================
    #  MISE À JOUR DU COMPTE
    # ==================================================================

    def update_account(self, account_info: dict):
        """Met à jour les informations du compte."""
        self.balance = float(account_info.get("balance", self.balance))
        self.equity = float(account_info.get("equity", self.equity))
        self.currency = account_info.get("currency", self.currency)

        # Mise à jour du pic d'équité
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        # Mise à jour du pic de session
        if self.balance > self.session_peak_balance:
            self.session_peak_balance = self.balance

        # Vérification de la courbe d'équité
        self._check_equity_curve()

        # Vérification du mode récupération
        self._check_recovery_mode()

        self._reset_daily_if_needed()

    # ==================================================================
    #  ENREGISTREMENT D'UN TRADE
    # ==================================================================

    def record_trade(self, pnl: float, symbol: str = ""):
        """
        Enregistre le résultat d'un trade fermé.

        Args:
            pnl: Résultat du trade en devise du compte
            symbol: Symbole du trade (optionnel, pour suivi par symbole)
        """
        self._reset_daily_if_needed()

        self.daily_pnl += pnl
        self.daily_trade_count += 1

        # Suivi par symbole
        if symbol:
            self.daily_symbol_pnl[symbol] = self.daily_symbol_pnl.get(symbol, 0.0) + pnl
            self.daily_symbol_trades[symbol] = self.daily_symbol_trades.get(symbol, 0) + 1

        # Mise à jour des compteurs de séries
        if pnl > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            # Confirmation de récupération
            if self.recovery_mode:
                self.recovery_confirmation_count += 1
                logger.info(
                    f"mode récupération : confirmation {self.recovery_confirmation_count}/"
                    f"{self.recovery_confirmation_trades}"
                )
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

            # Vérification si pause forcée nécessaire
            if self.consecutive_losses >= self.max_consecutive_losses:
                self._trigger_pause(
                    f"{self.consecutive_losses} pertes consécutives atteintes "
                    f"(limite={self.max_consecutive_losses})"
                )

            # Vérification si le mode récupération doit s'activer
            if abs(pnl) > self.balance * (self.recovery_mode_threshold_pct / 100.0):
                self._activate_recovery_mode(abs(pnl))

        # Historique récent pour win rate dynamique
        self.recent_trades.append(pnl)
        if len(self.recent_trades) > self.win_rate_window:
            self.recent_trades = self.recent_trades[-self.win_rate_window:]

        logger.info(
            f"trade enregistré : PnL={pnl:+.2f} {self.currency} | "
            f"PnL quotidien={self.daily_pnl:+.2f} | "
            f"trades aujourd'hui={self.daily_trade_count} | "
            f"série perdante={self.consecutive_losses} | "
            f"série gagnante={self.consecutive_wins}"
            + (f" | symbole={symbol}" if symbol else "")
        )

    # ==================================================================
    #  VÉRIFICATIONS DE RISQUE
    # ==================================================================

    def can_open_position(self, current_positions: int, symbol: str = "") -> Tuple[bool, str]:
        """
        Vérifie si on peut ouvrir une nouvelle position.

        Args:
            current_positions: Nombre actuel de positions ouvertes
            symbol: Symbole de la position envisagée (optionnel)

        Returns:
            (autorisé, raison) - True si autorisé, False avec raison sinon
        """
        self._reset_daily_if_needed()

        # Vérification de la pause forcée
        if self.paused_until is not None:
            if datetime.now() < self.paused_until:
                remaining = (self.paused_until - datetime.now()).seconds // 60
                return False, (
                    f"pause forcée active - raison : {self.pause_reason} "
                    f"(reste {remaining} min)"
                )
            else:
                logger.info("pause forcée terminée, reprise du trading")
                self.paused_until = None
                self.pause_reason = ""
                self.consecutive_losses = 0

        # Vérification du nombre max de positions
        if current_positions >= self.max_positions:
            return False, (
                f"nombre max de positions atteint "
                f"({current_positions}/{self.max_positions})"
            )

        # Vérification du nombre max de positions par symbole
        if symbol:
            sym_positions = self.daily_positions_per_symbol.get(symbol, 0)
            if sym_positions >= self.max_positions_per_symbol:
                return False, (
                    f"nombre max de positions par symbole atteint pour {symbol} "
                    f"({sym_positions}/{self.max_positions_per_symbol})"
                )

        # Vérification du nombre max de trades quotidiens
        if self.daily_trade_count >= self.max_daily_trades:
            return False, (
                f"nombre max de trades quotidiens atteint "
                f"({self.daily_trade_count}/{self.max_daily_trades})"
            )

        # Vérification de la perte quotidienne maximale
        max_daily_loss = self.balance * (self.max_daily_loss_pct / 100.0)
        if self.daily_pnl <= -max_daily_loss:
            return False, (
                f"perte quotidienne maximale atteinte "
                f"({self.daily_pnl:.2f}/{-max_daily_loss:.2f})"
            )

        # Vérification du drawdown de session
        session_drawdown = (
            (self.session_peak_balance - self.balance) / self.session_peak_balance * 100
            if self.session_peak_balance > 0 else 0
        )
        if session_drawdown >= self.max_session_drawdown_pct:
            return False, (
                f"drawdown de session maximal atteint "
                f"({session_drawdown:.1f}%/{self.max_session_drawdown_pct}%)"
            )

        # Vérification de la limite de perte par symbole
        if symbol:
            sym_loss = abs(min(0.0, self.daily_symbol_pnl.get(symbol, 0.0)))
            limit = self._get_symbol_loss_limit(symbol)
            if sym_loss >= limit:
                return False, (
                    f"limite de perte quotidienne atteinte pour {symbol} "
                    f"({sym_loss:.2f}/{limit:.2f})"
                )

        # Vérification de l'exposition totale
        if self.max_total_exposure > 0 and self.current_total_exposure >= self.max_total_exposure:
            return False, (
                f"exposition totale maximale atteinte "
                f"({self.current_total_exposure:.0f}/{self.max_total_exposure:.0f})"
            )

        # Vérification de l'alerte courbe d'équité
        if self.equity_alert_triggered:
            return False, "alerte courbe d'équité active - trading suspendu"

        return True, "OK"

    # ==================================================================
    #  CALCUL DE LA TAILLE DE POSITION (LOT)
    # ==================================================================

    def calculate_lot_size(self, stop_loss_pips: float, pip_value: float = 10.0) -> float:
        """
        Calcule la taille du lot basée sur le risque par trade.
        Intègre le dimensionnement dynamique basé sur le win rate,
        le mode récupération, et la réduction pendant les actualités.

        Args:
            stop_loss_pips: Distance du stop-loss en pips
            pip_value: Valeur d'un pip pour 1 lot standard (défaut 10 USD pour forex)

        Returns:
            Taille du lot arrondie au lot_step
        """
        if stop_loss_pips <= 0:
            logger.warning("stop_loss_pips <= 0, utilisation du lot par défaut")
            return self.default_lot

        max_risk_amount = self.balance * (self.max_risk_pct / 100.0)
        lot = max_risk_amount / (stop_loss_pips * pip_value)

        # --- Dimensionnement dynamique basé sur le win rate ---
        wr_multiplier = self._get_win_rate_multiplier()
        lot *= wr_multiplier

        # --- Réduction en mode récupération ---
        if self.recovery_mode:
            lot *= self.recovery_size_factor
            logger.info(
                f"mode récupération actif : lot réduit par facteur "
                f"{self.recovery_size_factor:.2f}"
            )

        # --- Réduction pendant les heures d'actualités ---
        news_mult = self._get_news_risk_multiplier()
        if news_mult < 1.0:
            lot *= news_mult
            logger.info(
                f"réduction de risque pendant les actualités : "
                f"facteur {news_mult:.2f}"
            )

        # Arrondir au lot_step
        lot = round(lot / self.lot_step) * self.lot_step

        # Limiter entre min et max
        lot = max(self.min_lot, min(self.max_lot, lot))

        logger.info(
            f"calcul lot : risque max {max_risk_amount:.2f} {self.currency} | "
            f"SL {stop_loss_pips:.1f} pips | WR x{wr_multiplier:.2f} | "
            f"lot final = {lot:.2f}"
        )
        return lot

    # ==================================================================
    #  CALCUL STOP-LOSS ET TAKE-PROFIT
    # ==================================================================

    def calculate_sl_tp(self, entry_price: float, signal_direction: str,
                         atr_value: Optional[float] = None,
                         pip_size: float = 0.0001) -> Tuple[float, float]:
        """
        Calcule les niveaux de stop-loss et take-profit.

        Args:
            entry_price: Prix d'entrée
            signal_direction: 'BUY' ou 'SELL'
            atr_value: Valeur ATR pour SL/TP dynamique (optionnel)
            pip_size: Taille d'un pip (0.0001 pour forex majeur, 0.01 pour XAUUSD)

        Returns:
            (stop_loss_price, take_profit_price)
        """
        if self.use_dynamic_sl_tp and atr_value is not None and atr_value > 0:
            sl_distance = atr_value * self.atr_sl_mult
            tp_distance = atr_value * self.atr_tp_mult
        else:
            sl_distance = self.default_sl_pips * pip_size
            tp_distance = self.default_tp_pips * pip_size

        if signal_direction == "BUY":
            sl = entry_price - sl_distance
            tp = entry_price + tp_distance
        else:  # SELL
            sl = entry_price + sl_distance
            tp = entry_price - tp_distance

        logger.info(
            f"SL/TP calculés : entrée={entry_price:.5f} | "
            f"SL={sl:.5f} ({sl_distance/pip_size:.1f} pips) | "
            f"TP={tp:.5f} ({tp_distance/pip_size:.1f} pips)"
        )
        return sl, tp

    # ==================================================================
    #  TRAILING STOP (CLASSIQUE + ATR)
    # ==================================================================

    def check_trailing_stop(self, position: dict, current_price: float,
                            pip_size: float = 0.0001,
                            atr_value: Optional[float] = None) -> Optional[float]:
        """
        Vérifie si le trailing stop doit être déplacé.
        Supporte le trailing classique en pips et le trailing basé sur l'ATR.

        Args:
            position: Dictionnaire de la position MT5
            current_price: Prix actuel du marché
            pip_size: Taille d'un pip
            atr_value: Valeur ATR actuelle (optionnel, pour trailing ATR)

        Returns:
            Nouveau SL si modification nécessaire, None sinon.
        """
        if not self.use_trailing_stop:
            return None

        pos_type = position.get("type")
        current_sl = float(position.get("sl", 0))
        open_price = float(position.get("price_open", 0))
        profit_pips = 0

        # Détermination de la distance de trailing
        if self.use_atr_trailing and atr_value is not None and atr_value > 0:
            trailing_distance = atr_value * self.atr_trailing_mult
            logger.debug(
                f"trailing stop ATR activé : distance = {trailing_distance:.5f} "
                f"(ATR={atr_value:.5f} x {self.atr_trailing_mult})"
            )
        else:
            trailing_distance = self.trailing_stop_pips * pip_size

        if pos_type == 0:  # BUY
            profit_pips = (current_price - open_price) / pip_size
            if profit_pips >= self.trailing_stop_pips:
                new_sl = current_price - trailing_distance
                if new_sl > current_sl:
                    logger.info(
                        f"trailing stop BUY : profit {profit_pips:.1f} pips, "
                        f"SL {current_sl:.5f} -> {new_sl:.5f}"
                    )
                    return new_sl
        elif pos_type == 1:  # SELL
            profit_pips = (open_price - current_price) / pip_size
            if profit_pips >= self.trailing_stop_pips:
                new_sl = current_price + trailing_distance
                if new_sl < current_sl or current_sl == 0:
                    logger.info(
                        f"trailing stop SELL : profit {profit_pips:.1f} pips, "
                        f"SL {current_sl:.5f} -> {new_sl:.5f}"
                    )
                    return new_sl

        return None

    def should_partial_close(self, position: dict, current_price: float,
                              pip_size: float = 0.0001) -> bool:
        """
        Vérifie si une fermeture partielle de la position doit être effectuée.

        Args:
            position: Dictionnaire de la position MT5
            current_price: Prix actuel
            pip_size: Taille d'un pip

        Returns:
            True si une fermeture partielle est recommandée
        """
        if self.partial_close_pct <= 0:
            return False

        pos_type = position.get("type")
        open_price = float(position.get("price_open", 0))

        if pos_type == 0:  # BUY
            profit_pips = (current_price - open_price) / pip_size
        elif pos_type == 1:  # SELL
            profit_pips = (open_price - current_price) / pip_size
        else:
            return False

        if profit_pips >= self.partial_close_pips:
            logger.info(
                f"fermeture partielle recommandée : profit {profit_pips:.1f} pips >= "
                f"seuil {self.partial_close_pips:.0f} pips | "
                f"quantité à fermer = {self.partial_close_pct:.0%}"
            )
            return True

        return False

    # ==================================================================
    #  BREAKEVEN
    # ==================================================================

    def check_breakeven(self, position: dict, current_price: float,
                        pip_size: float = 0.0001) -> Optional[float]:
        """
        Vérifie si le SL doit être déplacé au breakeven.

        Args:
            position: Dictionnaire de la position MT5
            current_price: Prix actuel du marché
            pip_size: Taille d'un pip

        Returns:
            Nouveau SL au breakeven si condition remplie, None sinon.
        """
        pos_type = position.get("type")
        current_sl = float(position.get("sl", 0))
        open_price = float(position.get("price_open", 0))
        be_price = open_price + self.move_sl_to_be_pips * pip_size

        if pos_type == 0:  # BUY
            profit_pips = (current_price - open_price) / pip_size
            if profit_pips >= self.break_even_after_pips:
                if current_sl < be_price:
                    logger.info(
                        f"breakeven BUY : profit {profit_pips:.1f} pips, "
                        f"SL -> {be_price:.5f}"
                    )
                    return be_price
        elif pos_type == 1:  # SELL
            profit_pips = (open_price - current_price) / pip_size
            if profit_pips >= self.break_even_after_pips:
                be_price = open_price - self.move_sl_to_be_pips * pip_size
                if current_sl > be_price or current_sl == 0:
                    logger.info(
                        f"breakeven SELL : profit {profit_pips:.1f} pips, "
                        f"SL -> {be_price:.5f}"
                    )
                    return be_price

        return None

    # ==================================================================
    #  SCORE DE RISQUE (0-100)
    # ==================================================================

    def get_risk_score(self) -> int:
        """
        Calcule un score de risque global de 0 (sûr) à 100 (danger critique).

        Le score est calculé à partir de multiples facteurs :
          - Perte quotidienne (% de la limite)
          - Pertes consécutives
          - Drawdown de session
          - Courbe d'équité
          - Mode récupération actif
          - Exposition totale
          - Win rate récent

        Returns:
            Score de risque entier entre 0 et 100
        """
        score = 0

        # --- Perte quotidienne (0-25 points) ---
        if self.balance > 0:
            daily_loss_pct = abs(min(0.0, self.daily_pnl)) / self.balance * 100
            daily_limit = self.max_daily_loss_pct
            daily_ratio = min(1.0, daily_loss_pct / daily_limit) if daily_limit > 0 else 0
            score += int(daily_ratio * 25)

        # --- Pertes consécutives (0-20 points) ---
        cl_ratio = min(1.0, self.consecutive_losses / max(1, self.max_consecutive_losses))
        score += int(cl_ratio * 20)

        # --- Drawdown de session (0-15 points) ---
        if self.session_peak_balance > 0:
            session_dd = (
                (self.session_peak_balance - self.balance)
                / self.session_peak_balance * 100
            )
            dd_ratio = min(1.0, session_dd / max(1, self.max_session_drawdown_pct))
            score += int(dd_ratio * 15)

        # --- Courbe d'équité (0-15 points) ---
        if self.peak_equity > 0:
            equity_drop = (self.peak_equity - self.equity) / self.peak_equity * 100
            eq_ratio = min(1.0, equity_drop / max(1, self.equity_curve_alert_pct))
            score += int(eq_ratio * 15)

        # --- Mode récupération (10 points si actif) ---
        if self.recovery_mode:
            score += 10

        # --- Exposition totale (0-10 points) ---
        if self.max_total_exposure > 0:
            exp_ratio = min(1.0, self.current_total_exposure / self.max_total_exposure)
            score += int(exp_ratio * 10)

        # --- Win rate récent : réduit le score si WR est bon (0-5 points bonus négatif) ---
        recent_wr = self._get_recent_win_rate()
        if recent_wr > 0.6:
            score = max(0, score - 5)
        elif recent_wr < 0.4:
            score += 5

        return min(100, max(0, score))

    def get_risk_score_label(self) -> str:
        """
        Retourne une étiquette descriptive du score de risque.

        Returns:
            Chaîne descriptive du niveau de risque
        """
        score = self.get_risk_score()
        if score <= 15:
            return "faible - conditions optimales"
        elif score <= 30:
            return "modéré - normal"
        elif score <= 50:
            return "élevé - vigilance requise"
        elif score <= 75:
            return "critique - réduire l'exposition"
        else:
            return "extrême - arrêt recommandé"

    # ==================================================================
    #  GESTION DES POSITIONS PAR SYMBOLE
    # ==================================================================

    def register_symbol_position(self, symbol: str):
        """
        Enregistre l'ouverture d'une position sur un symbole.

        Args:
            symbol: Symbole de la position ouverte
        """
        self._reset_daily_if_needed()
        self.daily_positions_per_symbol[symbol] = (
            self.daily_positions_per_symbol.get(symbol, 0) + 1
        )

    def unregister_symbol_position(self, symbol: str):
        """
        Enregistre la fermeture d'une position sur un symbole.

        Args:
            symbol: Symbole de la position fermée
        """
        if symbol in self.daily_positions_per_symbol:
            self.daily_positions_per_symbol[symbol] = max(
                0, self.daily_positions_per_symbol[symbol] - 1
            )

    def set_total_exposure(self, exposure: float):
        """
        Met à jour l'exposition totale actuelle.

        Args:
            exposure: Valeur notionnelle totale des positions ouvertes
        """
        self.current_total_exposure = exposure

    # ==================================================================
    #  RAPPORT DE RISQUE
    # ==================================================================

    def get_risk_report(self) -> Dict:
        """Retourne un rapport complet de l'état actuel du risque."""
        self._reset_daily_if_needed()

        # Calcul du drawdown de session
        session_drawdown_pct = 0.0
        if self.session_peak_balance > 0:
            session_drawdown_pct = (
                (self.session_peak_balance - self.balance)
                / self.session_peak_balance * 100
            )

        # Calcul du drawdown d'équité
        equity_drawdown_pct = 0.0
        if self.peak_equity > 0:
            equity_drawdown_pct = (
                (self.peak_equity - self.equity) / self.peak_equity * 100
            )

        return {
            # État du compte
            "balance": self.balance,
            "equity": self.equity,
            "currency": self.currency,
            "peak_equity": self.peak_equity,

            # Performance quotidienne
            "daily_pnl": self.daily_pnl,
            "daily_pnl_pct": (
                (self.daily_pnl / self.balance * 100) if self.balance > 0 else 0
            ),
            "daily_trade_count": self.daily_trade_count,
            "max_daily_loss": self.balance * (self.max_daily_loss_pct / 100.0),
            "remaining_daily_trades": max(
                0, self.max_daily_trades - self.daily_trade_count
            ),
            "risk_per_trade_amount": self.balance * (self.max_risk_pct / 100.0),

            # Séries
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,

            # Drawdown
            "session_drawdown_pct": round(session_drawdown_pct, 2),
            "max_session_drawdown_pct": self.max_session_drawdown_pct,
            "equity_drawdown_pct": round(equity_drawdown_pct, 2),
            "equity_curve_alert_pct": self.equity_curve_alert_pct,

            # Mode récupération
            "recovery_mode": self.recovery_mode,
            "recovery_confirmation_count": self.recovery_confirmation_count,
            "recovery_confirmation_needed": self.recovery_confirmation_trades,

            # Pause
            "paused": self.paused_until is not None,
            "pause_reason": self.pause_reason if self.paused_until else "",

            # Exposition
            "current_total_exposure": self.current_total_exposure,
            "max_total_exposure": self.max_total_exposure,

            # Win rate récent
            "recent_win_rate": round(self._get_recent_win_rate() * 100, 1),
            "recent_trades_count": len(self.recent_trades),

            # Score de risque
            "risk_score": self.get_risk_score(),
            "risk_score_label": self.get_risk_score_label(),

            # Perte par symbole
            "symbol_daily_pnl": dict(self.daily_symbol_pnl),

            # Actualités
            "news_risk_reduction_active": self._is_news_hour(),
        }

    # ==================================================================
    #  MÉTHODES PRIVÉES
    # ==================================================================

    def _get_recent_win_rate(self) -> float:
        """
        Calcule le win rate sur les derniers trades.

        Returns:
            Win rate entre 0.0 et 1.0
        """
        if not self.recent_trades:
            return 0.5  # Neutre par défaut
        wins = sum(1 for t in self.recent_trades if t > 0)
        return wins / len(self.recent_trades)

    def _get_win_rate_multiplier(self) -> float:
        """
        Calcule un multiplicateur de taille basé sur le win rate récent.
        Augmente la taille quand WR > 60%, réduit quand WR < 40%.

        Returns:
            Multiplicateur entre 0.6 et 1.4
        """
        wr = self._get_recent_win_rate()
        if wr > 0.6:
            # Augmentation progressive jusqu'à +40% pour WR parfait
            multiplier = 1.0 + (wr - 0.6) / 0.4 * 0.4
            logger.debug(
                f"multiplicateur win rate haussier : x{multiplier:.2f} (WR={wr:.1%})"
            )
        elif wr < 0.4:
            # Réduction progressive jusqu'à -40% pour WR nul
            multiplier = 1.0 - (0.4 - wr) / 0.4 * 0.4
            logger.debug(
                f"multiplicateur win rate baissier : x{multiplier:.2f} (WR={wr:.1%})"
            )
        else:
            multiplier = 1.0
        return max(0.6, min(1.4, multiplier))

    def _is_news_hour(self) -> bool:
        """
        Vérifie si l'heure actuelle est dans une plage d'actualités.

        Returns:
            True si on est dans une période d'actualités à fort impact
        """
        if not self.news_risk_reduction_enabled:
            return False
        now = datetime.now().time()
        for start_hour, end_hour in self.news_hours:
            if time(start_hour) <= now <= time(end_hour):
                return True
        return False

    def _get_news_risk_multiplier(self) -> float:
        """
        Retourne le facteur de réduction pendant les heures d'actualités.

        Returns:
            Facteur entre news_risk_factor et 1.0
        """
        if self._is_news_hour():
            return self.news_risk_factor
        return 1.0

    def _check_equity_curve(self):
        """
        Surveille la courbe d'équité et déclenche une alerte
        si l'équité tombe en dessous du seuil depuis le pic.
        """
        if self.peak_equity <= 0:
            return
        drop_pct = (self.peak_equity - self.equity) / self.peak_equity * 100
        if drop_pct >= self.equity_curve_alert_pct and not self.equity_alert_triggered:
            self.equity_alert_triggered = True
            self.equity_alert_time = datetime.now()
            logger.warning(
                f"ALERTE COURBE D'ÉQUITÉ : l'équité a chuté de {drop_pct:.1f}% "
                f"depuis le pic ({self.peak_equity:.2f} -> {self.equity:.2f}). "
                f"Trading suspendu jusqu'à récupération."
            )
        elif drop_pct < self.equity_curve_alert_pct * 0.5 and self.equity_alert_triggered:
            self.equity_alert_triggered = False
            logger.info(
                f"alerte courbe d'équité levée : "
                f"drop réduit à {drop_pct:.1f}%"
            )

    def _check_recovery_mode(self):
        """
        Vérifie si le mode récupération doit être désactivé
        après suffisamment de trades de confirmation.
        """
        if not self.recovery_mode:
            return
        if self.recovery_confirmation_count >= self.recovery_confirmation_trades:
            logger.info(
                f"mode récupération désactivé après "
                f"{self.recovery_confirmation_count} trades de confirmation"
            )
            self.recovery_mode = False
            self.recovery_confirmation_count = 0
            self.recovery_trigger_time = None

    def _activate_recovery_mode(self, loss_amount: float):
        """
        Active le mode récupération après une grosse perte.

        Args:
            loss_amount: Montant de la perte en devise du compte
        """
        if self.recovery_mode:
            return  # Déjà en mode récupération
        self.recovery_mode = True
        self.recovery_confirmation_count = 0
        self.recovery_trigger_time = datetime.now()
        self.last_big_loss_amount = loss_amount
        logger.warning(
            f"MODE RÉCUPÉRATION ACTIVÉ : perte importante de {loss_amount:.2f} {self.currency} | "
            f"taille réduite à {self.recovery_size_factor:.0%} | "
            f"{self.recovery_confirmation_trades} trades gagnants requis pour sortir"
        )

    def _trigger_pause(self, reason: str):
        """
        Déclenche une pause forcée du trading.

        Args:
            reason: Raison de la pause
        """
        # Pause de 30 minutes par défaut
        from datetime import timedelta
        pause_minutes = self.config["risk_management"].get("pause_duration_minutes", 30)
        self.paused_until = datetime.now() + timedelta(minutes=pause_minutes)
        self.pause_reason = reason
        logger.warning(
            f"PAUSE FORCÉE déclenchée : {reason} | "
            f"reprise prévue à {self.paused_until.strftime('%H:%M:%S')}"
        )

    def _get_symbol_loss_limit(self, symbol: str) -> float:
        """
        Retourne la limite de perte quotidienne pour un symbole.

        Args:
            symbol: Symbole à vérifier

        Returns:
            Limite de perte en devise du compte
        """
        # Limite spécifique au symbole si définie
        if symbol in self.symbol_daily_loss_limits:
            return self.symbol_daily_loss_limits[symbol]
        # Sinon, pourcentage du solde
        return self.balance * (self.default_symbol_daily_loss_pct / 100.0)

    def reset_session(self):
        """Réinitialise les compteurs de session (drawdown de session, etc.)."""
        self.session_start_balance = self.balance
        self.session_peak_balance = self.balance
        logger.info("compteurs de session réinitialisés")
