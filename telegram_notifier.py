"""
Notifications Telegram v4 pour le robot de trading.
=======================================================
Ameliorations v4 :
  1. Suivi du PnL en temps reel avec historique
  2. Notifications de series de gains/pertes (streaks)
  3. Alertes de connexion/deconnexion
  4. Progression de la croissance composee
  5. Alertes de risque (score eleve, limites approchees)
  6. Notifications de montee/descente de niveau compose
  7. Rapport hebdomadaire de performance
  8. Resume tous les 20 trades
  9. Notifications de changement de mode (pause/reprise/switch)
  10. Clavier inline sur les notifications de trade
  11. Surveillance de la qualite de connexion
  12. Mode silencieux pour notifications basse priorite
  13. Rate limiting pour eviter le spam
  14. Formatage ameliore avec emojis et sections

Utilisation :
  1. Cree un bot Telegram via @BotFather
  2. Obtiens le token du bot
  3. Obtiens ton chat_id via @userinfobot
  4. Configure dans config.json : telegram.bot_token et telegram.chat_id

Messages envoyes :
  - Demarrage/arret du robot
  - Ouverture de position (avec score, confluence, confidence)
  - Fermeture de position (+ PnL, duree, raison)
  - Rapport quotidien (balance, PnL, win rate, drawdown)
  - Rapport hebdomadaire (resume de la semaine)
  - Alertes de risque (score eleve, limite approchee)
  - Alertes d'erreur avec contexte
  - News filter (news a eviter, symboles affectes)
  - Progression croissance composee (niveau actuel, prochain palier)
  - Montee/descente de niveau compose
  - Alertes de series (3 wins d'affilee, 2 losses...)
  - Resume tous les 20 trades
  - Changement de mode (pause/reprise/switch broker)
  - Qualite de connexion
"""

import logging
import json
import os
import ssl
import urllib.request
import urllib.error
import urllib.parse
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

try:
    import certifi
except ImportError:
    certifi = None

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Envoie des notifications Telegram avec suivi PnL, alertes risque et commandes."""

    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, config: dict):
        self.config = config
        tg = config.get("telegram", {})
        self.enabled = tg.get("enabled", False)
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", str(tg.get("chat_id", "")))
        self.notify_trades = tg.get("notify_trades", True)
        self.notify_pnl = tg.get("notify_pnl", True)
        self.notify_errors = tg.get("notify_errors", True)
        self.notify_reports = tg.get("notify_reports", True)
        self.notify_news = tg.get("notify_news", True)
        self.notify_streaks = tg.get("notify_streaks", True)
        self.notify_risk = tg.get("notify_risk", True)
        self.notify_mode_changes = tg.get("notify_mode_changes", True)
        self.silent_hours = tg.get("silent_hours", None)
        self.rate_limit_seconds = tg.get("rate_limit_seconds", 3)
        self.low_priority_silent = tg.get("low_priority_silent", False)
        self.last_report_date = None
        self.last_weekly_report_date = None

        # Suivi PnL
        self._daily_pnl = 0.0
        self._trade_count = 0
        self._win_count = 0
        self._loss_count = 0
        self._current_streak = 0  # positif = wins, negatif = losses
        self._best_streak = 0
        self._worst_streak = 0
        self._last_send_time = 0
        self._session_start = datetime.now()
        self._total_pnl = 0.0  # PnL total toutes sessions confondues
        self._weekly_pnl = 0.0
        self._weekly_trades = 0
        self._weekly_wins = 0
        self._weekly_losses = 0
        self._last_risk_alert_time = 0
        self._last_connection_alert_time = 0
        self._trade_summary_interval = 20

        # Suivi connexion
        self._connection_failures = {}  # broker -> nombre d'echecs consecutifs
        self._last_quality_alert = 0

        self._test_connection()

    @staticmethod
    def _ssl_context():
        if certifi is not None:
            return ssl.create_default_context(cafile=certifi.where())
        return ssl.create_default_context()

    def _urlopen(self, request, timeout):
        return urllib.request.urlopen(request, timeout=timeout, context=self._ssl_context())

    def _test_connection(self):
        if not self.enabled:
            return
        if not self.token or not self.chat_id:
            logger.warning("Telegram: token ou chat_id manquant. Notifications desactivees.")
            self.enabled = False
            return
        if self.token == "VOTRE_TOKEN_TELEGRAM_ICI":
            logger.info("Telegram: token non configure. Notifications desactivees.")
            self.enabled = False
            return
        try:
            self._send_message(
                "\U0001f916 <b>Robot Trading v4</b> connecte!\n\n"
                "Le bot surveille les marches.\n"
                f"Demarre a {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                "\U0001f4e1 Utilise /help pour les commandes."
            )
            logger.info("Telegram: notifications activees.")
        except Exception as e:
            logger.warning(f"Telegram: connexion echouee ({e}). Notifications desactivees.")
            self.enabled = False

    def _send_message(self, text: str, parse_mode: str = "HTML",
                       reply_markup: str = None, low_priority: bool = False) -> bool:
        if not self.enabled:
            return False

        # Mode silencieux basse priorite
        if low_priority and self.low_priority_silent:
            logger.debug("Telegram: notification basse priorite supprimee.")
            return False

        # Mode silencieux (pas de notif la nuit)
        if self._is_silent_hours():
            logger.debug("Telegram: mode silencieux, message non envoye.")
            return False

        # Rate limiting
        now = time.time()
        elapsed = now - self._last_send_time
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)

        try:
            url = self.TELEGRAM_API.format(token=self.token)
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": "true",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                                          headers={"Content-Type": "application/json"})
            with self._urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if not result.get("ok"):
                    logger.debug(f"Telegram API error: {result}")
                    return False
                self._last_send_time = time.time()
                return True
        except Exception as e:
            logger.debug(f"Telegram envoi echoue: {e}")
            return False

    def _is_silent_hours(self) -> bool:
        if not self.silent_hours:
            return False
        hour = datetime.now().hour
        start = self.silent_hours.get("start", 23)
        end = self.silent_hours.get("end", 7)
        if start > end:  # ex: 23h a 7h
            return hour >= start or hour < end
        else:
            return start <= hour < end

    def _build_trade_keyboard(self) -> str:
        """Construit un clavier inline pour actions rapides sur un trade."""
        keyboard = [
            [{"text": "\U0001f4ca Statut", "callback_data": "show_status"},
             {"text": "\U0001f4c8 Positions", "callback_data": "show_positions"}],
            [{"text": "\u23f8\ufe0f Pause", "callback_data": "pause_bot"},
             {"text": "\U0001f4dc Trades", "callback_data": "show_trades"}],
        ]
        return json.dumps({"inline_keyboard": keyboard})

    # ------------------------------------------------------------------
    #  SUIVI PnL
    # ------------------------------------------------------------------

    def record_trade_result(self, pnl: float):
        """Enregistre le resultat d'un trade pour le suivi."""
        self._daily_pnl += pnl
        self._total_pnl += pnl
        self._weekly_pnl += pnl
        self._trade_count += 1
        self._weekly_trades += 1
        if pnl > 0:
            self._win_count += 1
            self._weekly_wins += 1
            self._current_streak = max(0, self._current_streak) + 1
            self._best_streak = max(self._best_streak, self._current_streak)
        else:
            self._loss_count += 1
            self._weekly_losses += 1
            self._current_streak = min(0, self._current_streak) - 1
            self._worst_streak = min(self._worst_streak, self._current_streak)

        # Alerte de serie
        if self.notify_streaks and self.enabled:
            if self._current_streak == 3:
                self._send_message(
                    f"\U0001f525 <b>SERIE DE 3 WINS!</b>\n"
                    f"PnL jour: {self._daily_pnl:+.2f}$\n"
                    f"PnL total: {self._total_pnl:+.2f}$\n"
                    f"Continue comme ca!"
                )
            elif self._current_streak == 5:
                self._send_message(
                    f"\U0001f525\U0001f525 <b>SERIE INCROYABLE DE 5 WINS!</b>\n"
                    f"PnL jour: {self._daily_pnl:+.2f}$\n"
                    f"Performance exceptionnelle!"
                )
            elif self._current_streak == -2:
                self._send_message(
                    f"\u26a0\ufe0f <b>2 pertes consecutives</b>\n"
                    f"PnL jour: {self._daily_pnl:+.2f}$\n"
                    f"Le bot reste prudent."
                )
            elif self._current_streak == -3:
                self._send_message(
                    f"\U0001f6a8 <b>3 pertes consecutives!</b>\n"
                    f"PnL jour: {self._daily_pnl:+.2f}$\n"
                    f"Le risque est automatiquement reduit."
                )

        # Resume tous les 20 trades
        if self._trade_count > 0 and self._trade_count % self._trade_summary_interval == 0:
            self._send_trade_summary()

    def _send_trade_summary(self):
        """Envoie un resume tous les 20 trades."""
        wr = (self._win_count / self._trade_count * 100) if self._trade_count > 0 else 0
        avg_pnl = self._total_pnl / self._trade_count if self._trade_count > 0 else 0
        text = (
            f"\U0001f4ca <b>RESUME TOUS LES {self._trade_summary_interval} TRADES</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4c9 Total trades : <b>{self._trade_count}</b>\n"
            f"\u2705 Wins : <b>{self._win_count}</b> | \u274c Losses : <b>{self._loss_count}</b>\n"
            f"\U0001f3af Win Rate : <b>{wr:.1f}%</b>\n"
            f"\U0001f4b8 PnL total : <b>{self._total_pnl:+.2f}$</b>\n"
            f"\U0001f4c8 PnL moyen : <b>{avg_pnl:+.2f}$</b> par trade\n"
            f"\U0001f525 Meilleure serie : {self._best_streak}W\n"
            f"\U0001f4a2 Pire serie : {abs(self._worst_streak)}L\n"
            f"\U0001f552 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )
        self._send_message(text)

    def reset_daily(self):
        """Remet a zero les stats quotidiennes."""
        self._daily_pnl = 0.0
        self._trade_count = 0
        self._win_count = 0
        self._loss_count = 0
        self._current_streak = 0

    def reset_weekly(self):
        """Remet a zero les stats hebdomadaires."""
        self._weekly_pnl = 0.0
        self._weekly_trades = 0
        self._weekly_wins = 0
        self._weekly_losses = 0

    # ------------------------------------------------------------------
    #  NOTIFICATIONS TRADES
    # ------------------------------------------------------------------

    def notify_trade_open(self, symbol: str, direction: str,
                           stake: float, confidence: float,
                           market_score: float = 0,
                           mtf_score: float = 0):
        if not self.enabled or not self.notify_trades:
            return
        emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"
        arrow = "\u2b06\ufe0f" if direction == "BUY" else "\u2b07\ufe0f"

        # Barre de confiance
        conf_bar_len = 10
        conf_filled = int(confidence / 100 * conf_bar_len)
        conf_bar = "\U0001f7e9" * conf_filled + "\u2b1c" * (conf_bar_len - conf_filled)

        text = (
            f"{emoji} <b>POSITION OUVERTE</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4c8 Actif: <b>{symbol}</b>\n"
            f"{arrow} Sens: <b>{direction}</b>\n"
            f"\U0001f4b0 Mise: <b>{stake:.2f}$</b>\n"
            f"\U0001f4ca Confiance: {confidence:.0f}% [{conf_bar}]\n"
        )
        if market_score > 0:
            score_bar_len = 10
            score_filled = int(market_score / 100 * score_bar_len)
            score_bar = "\U0001f7e9" * score_filled + "\u2b1c" * (score_bar_len - score_filled)
            text += f"\U0001f3af Score marche: {market_score:.0f}/100 [{score_bar}]\n"
        if mtf_score > 0:
            text += f"\U0001f504 Multi-TF: {mtf_score:.1f}/10\n"
        text += f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"

        self._send_message(text, reply_markup=self._build_trade_keyboard())

    def notify_trade_close(self, symbol: str, pnl: float,
                            reason: str = "",
                            duration_minutes: float = 0):
        if not self.enabled or not self.notify_pnl:
            return
        self.record_trade_result(pnl)

        if pnl > 0:
            emoji = "\u2705"
            result = f"<b>+{pnl:.2f}$</b>"
            result_emoji = "\U0001f389"
        elif pnl == 0:
            emoji = "\u23f8\ufe0f"
            result = f"<b>{pnl:.2f}$</b>"
            result_emoji = ""
        else:
            emoji = "\u274c"
            result = f"<b>{pnl:.2f}$</b>"
            result_emoji = ""

        # Pourcentage du capital
        pct_info = ""
        if self._trade_count > 0:
            avg = self._daily_pnl / self._trade_count if self._trade_count else 0
            pct_info = f"\n\U0001f4c8 PnL moyen/trade: {avg:+.2f}$"

        text = (
            f"{emoji} <b>POSITION FERMEE</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4c8 Actif: <b>{symbol}</b>\n"
            f"\U0001f4b8 PnL: {result} {result_emoji}\n"
        )
        if reason:
            text += f"\U0001f4a1 Raison: {reason}\n"
        if duration_minutes > 0:
            text += f"\u23f1 Duree: {duration_minutes:.0f} min\n"
        text += (
            f"\U0001f4c9 Jour: {self._daily_pnl:+.2f}$ "
            f"({self._win_count}W/{self._loss_count}L)"
            f"{pct_info}\n"
            f"\U0001f4c9 Total: {self._total_pnl:+.2f}$\n"
            f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send_message(text, reply_markup=self._build_trade_keyboard())

    # ------------------------------------------------------------------
    #  NOTIFICATIONS RISQUE
    # ------------------------------------------------------------------

    def notify_risk_alert(self, risk_score: float, risk_state: str,
                           details: str = "", broker: str = ""):
        """Notification d'alerte de risque."""
        if not self.enabled or not self.notify_risk:
            return
        # Eviter le spam (une alerte par minute max)
        now = time.time()
        if now - self._last_risk_alert_time < 60:
            return
        self._last_risk_alert_time = now

        if risk_score >= 80:
            emoji = "\U0001f6a8"
            level = "CRITIQUE"
        elif risk_score >= 60:
            emoji = "\u26a0\ufe0f"
            level = "ELEVE"
        elif risk_score >= 40:
            emoji = "\U0001f7e1"
            level = "MODERE"
        else:
            emoji = "\U0001f7e2"
            level = "FAIBLE"

        broker_tag = f" [{broker}]" if broker else ""
        text = (
            f"{emoji} <b>ALERTE RISQUE{broker_tag}</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f3af Score : <b>{risk_score:.0f}/100</b> ({level})\n"
            f"\U0001f4cb Etat : <b>{risk_state}</b>\n"
        )
        if details:
            text += f"\U0001f4dd Details : {details}\n"
        text += f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
        self._send_message(text)

    def notify_risk_limit_approaching(self, metric: str, current: float,
                                       limit: float, broker: str = ""):
        """Notification quand une limite de risque est approchee (80%+)."""
        if not self.enabled or not self.notify_risk:
            return
        now = time.time()
        if now - self._last_risk_alert_time < 120:
            return
        self._last_risk_alert_time = now

        pct = (current / limit * 100) if limit > 0 else 0
        if pct < 80:
            return  # Ne notifier que si on approche la limite

        emoji = "\U0001f6a8" if pct >= 95 else "\u26a0\ufe0f"
        broker_tag = f" [{broker}]" if broker else ""
        text = (
            f"{emoji} <b>LIMITE DE RISQUE APPROCHEE{broker_tag}</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4cb Metrique : <b>{metric}</b>\n"
            f"\U0001f4c8 Actuel : <b>{current:.2f}</b>\n"
            f"\U0001f6d1 Limite : <b>{limit:.2f}</b>\n"
            f"\U0001f4ca Utilisation : <b>{pct:.0f}%</b>\n"
            f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send_message(text)

    # ------------------------------------------------------------------
    #  NOTIFICATIONS CROISSANCE COMPOSEE
    # ------------------------------------------------------------------

    def notify_compound_level(self, balance: float, level_name: str,
                               next_level: str, progress_pct: float):
        """Notification de niveau de croissance composee atteint."""
        if not self.enabled:
            return
        # Barre de progression
        bar_len = 10
        filled = int(progress_pct / 100 * bar_len)
        bar = "\U0001f7e9" * filled + "\u2b1c" * (bar_len - filled)

        self._send_message(
            f"\U0001f3c6 <b>NIVEAU ATTEINT!</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4b0 Solde: <b>{balance:.2f}$</b>\n"
            f"\U0001f3af Niveau: <b>{level_name}</b>\n"
            f"\U0001f3af Prochain: {next_level}\n"
            f"\U0001f4c8 Progression: [{bar}] {progress_pct:.0f}%"
        )

    def notify_compound_level_up(self, old_level: str, new_level: str,
                                  balance: float, stake_increase: float = 0):
        """Notification de montee de niveau compose."""
        if not self.enabled:
            return
        self._send_message(
            f"\U0001f31f <b>NIVEAU SUPERIEUR ATTEINT!</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\u2b06\ufe0f <b>{old_level}</b> -> <b>{new_level}</b>\n"
            f"\U0001f4b0 Solde: <b>{balance:.2f}$</b>\n"
        )
        if stake_increase > 0:
            self._send_message(
                f"\U0001f4b0 Mise augmentee de <b>+{stake_increase:.2f}$</b>\n"
                f"\U0001f525 La croissance composee fait son effet!"
            )

    def notify_compound_level_down(self, old_level: str, new_level: str,
                                    balance: float):
        """Notification de descente de niveau compose."""
        if not self.enabled:
            return
        self._send_message(
            f"\U0001f4a2 <b>DESCENTE DE NIVEAU</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\u2b07\ufe0f <b>{old_level}</b> -> <b>{new_level}</b>\n"
            f"\U0001f4b0 Solde: <b>{balance:.2f}$</b>\n"
            f"\U0001f4c9 La mise a ete reduite pour proteger le capital."
        )

    # ------------------------------------------------------------------
    #  NOTIFICATIONS MODE
    # ------------------------------------------------------------------

    def notify_mode_change(self, action: str, details: str = ""):
        """Notification de changement de mode (pause/reprise/switch)."""
        if not self.enabled or not self.notify_mode_changes:
            return

        actions = {
            "pause": ("\u23f8\ufe0f <b>MODE PAUSE ACTIVE</b>",
                       "Le bot ne prend plus de nouvelles positions."),
            "resume": ("\u25b6\ufe0f <b>MODE TRADING REPRIS</b>",
                        "Le bot reprend le trading normalement."),
            "switch_broker": ("\U0001f504 <b>SWITCH DE BROKER</b>",
                                "Changement de broker effectue."),
        }
        title, subtitle = actions.get(action, (f"\u2139\ufe0f <b>{action.upper()}</b>", ""))

        text = (
            f"{title}\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{subtitle}\n"
        )
        if details:
            text += f"\U0001f4dd {details}\n"
        text += f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
        self._send_message(text)

    # ------------------------------------------------------------------
    #  NOTIFICATIONS CONNEXION
    # ------------------------------------------------------------------

    def notify_connection_status(self, broker: str, connected: bool):
        if not self.enabled:
            return

        now = time.time()
        if connected:
            failures = self._connection_failures.pop(broker, 0)
            if failures >= 2:
                self._send_message(
                    f"\U0001f7e2 <b>RECONNECTE</b>\n"
                    f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                    f"Connexion {broker} retablie apres {failures} tentative(s).\n"
                    f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
                )
            self._connection_failures[broker] = 0
            # Reset sur reconnexion normale
            if failures == 0:
                self._send_message(
                    f"\U0001f7e2 <b>CONNECTE</b>\n"
                    f"Connexion {broker} etablie.\n"
                    f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}",
                    low_priority=True
                )
        else:
            self._connection_failures[broker] = self._connection_failures.get(broker, 0) + 1
            failures = self._connection_failures[broker]
            if now - self._last_connection_alert_time < 30:
                return
            self._last_connection_alert_time = now
            self._send_message(
                f"\U0001f534 <b>DECONNECTE</b>\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"Connexion {broker} perdue! (tentative {failures})\n"
                f"Reconnexion automatique en cours...\n"
                f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
            )

    def notify_connection_quality(self, broker: str, latency_ms: float,
                                   success_rate: float, errors: int = 0):
        """Notification de surveillance de la qualite de connexion."""
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_quality_alert < 300:  # Max une alerte toutes les 5 min
            return

        issues = []
        if latency_ms > 2000:
            issues.append(f"\u26a0\ufe0f Latence elevee: <b>{latency_ms:.0f}ms</b>")
        if success_rate < 80:
            issues.append(f"\u26a0\ufe0f Taux de succes bas: <b>{success_rate:.0f}%</b>")
        if errors > 5:
            issues.append(f"\u26a0\ufe0f Erreurs recentes: <b>{errors}</b>")

        if not issues:
            return

        self._last_quality_alert = now
        text = (
            f"\U0001f4e1 <b>QUALITE CONNEXION {broker.upper()}</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            + "\n".join(issues) + "\n"
            + f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send_message(text, low_priority=True)

    # ------------------------------------------------------------------
    #  AUTRES NOTIFICATIONS
    # ------------------------------------------------------------------

    def notify_error(self, message: str):
        if not self.enabled or not self.notify_errors:
            return
        self._send_message(
            f"\u26a0\ufe0f <b>ERREUR</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{message}\n"
            f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
        )

    def notify_news_filter(self, reason: str):
        if not self.enabled or not self.notify_news:
            return
        self._send_message(
            f"\U0001f4f0 <b>FILTRE NEWS</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{reason}\n"
            f"\U0001f6ab Trade bloque pour securite."
        )

    def notify_daily_report(self, balance: float, daily_pnl: float,
                             total_trades: int, win_rate: float,
                             peak_balance: float, compound_msg: str,
                             max_drawdown: float = 0, profit_factor: float = 0):
        if not self.enabled or not self.notify_reports:
            return
        today = datetime.now().strftime("%d/%m/%Y")
        pnl_emoji = "\U0001f7e2" if daily_pnl >= 0 else "\U0001f534"

        # Performance emoji
        if daily_pnl > 10:
            perf = "\U0001f525 Excellente journee!"
        elif daily_pnl > 0:
            perf = "\U0001f44d Journee positive"
        elif daily_pnl == 0:
            perf = "\U0001f914 Journee neutre"
        elif daily_pnl > -5:
            perf = "\U0001f4aa Petite perte, ca va aller"
        else:
            perf = "\U0001f4a2 Journee difficile"

        uptime = str(datetime.now() - self._session_start).split(".")[0]

        text = (
            f"\U0001f4ca <b>RAPPORT JOURNALIER</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4c5 {today}\n"
            f"\U0001f552 Uptime: {uptime}\n"
            f"\n"
            f"\U0001f4c8 Solde: <b>{balance:.2f}$</b>\n"
            f"{pnl_emoji} PnL jour: <b>{daily_pnl:+.2f}$</b>\n"
            f"\U0001f4c9 Trades: {total_trades} | Win: {self._win_count} | Loss: {self._loss_count}\n"
            f"\U0001f3af Win Rate: <b>{win_rate:.1f}%</b>\n"
        )
        if max_drawdown > 0:
            text += f"\U0001f4c9 Max DD: {max_drawdown:.1f}%\n"
        if profit_factor > 0:
            text += f"\U0001f4b0 Profit Factor: {profit_factor:.2f}\n"
        text += (
            f"\U0001f4c8 Pic: {peak_balance:.2f}$\n"
            f"\n"
            f"\U0001f525 {compound_msg}\n"
            f"\n"
            f"{perf}"
        )
        self._send_message(text)
        self.last_report_date = today
        self.reset_daily()

    def notify_weekly_report(self, balance: float, weekly_pnl: float,
                              total_trades: int, win_rate: float,
                              peak_balance: float, max_drawdown: float = 0):
        """Rapport hebdomadaire de performance."""
        if not self.enabled or not self.notify_reports:
            return

        today = datetime.now().strftime("%d/%m/%Y")
        if self.last_weekly_report_date == today:
            return

        # Verifier si on est lundi (rapport de la semaine precedente)
        if datetime.now().weekday() != 0:
            return

        pnl_emoji = "\U0001f7e2" if weekly_pnl >= 0 else "\U0001f534"
        uptime = str(datetime.now() - self._session_start).split(".")[0]

        # Calculer le ROI
        roi = 0
        if peak_balance > 0:
            roi = (weekly_pnl / (peak_balance - weekly_pnl) * 100) if (peak_balance - weekly_pnl) > 0 else 0

        text = (
            f"\U0001f4ca <b>RAPPORT HEBDOMADAIRE</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f4c5 Semaine du {today}\n"
            f"\U0001f552 Uptime total: {uptime}\n"
            f"\n"
            f"\U0001f4c8 Solde: <b>{balance:.2f}$</b>\n"
            f"{pnl_emoji} PnL semaine: <b>{weekly_pnl:+.2f}$</b>\n"
            f"\U0001f4c9 Trades: {total_trades} | Win: {self._weekly_wins} | Loss: {self._weekly_losses}\n"
            f"\U0001f3af Win Rate: <b>{win_rate:.1f}%</b>\n"
            f"\U0001f4c8 ROI: <b>{roi:.1f}%</b>\n"
        )
        if max_drawdown > 0:
            text += f"\U0001f4c9 Max DD semaine: {max_drawdown:.1f}%\n"
        text += f"\U0001f4c8 Pic semaine: {peak_balance:.2f}$\n"

        self._send_message(text)
        self.last_weekly_report_date = today
        self.reset_weekly()

    def notify_info(self, message: str):
        if not self.enabled:
            return
        self._send_message(f"\u2139\ufe0f {message}", low_priority=True)

    def notify_mtf_block(self, symbol: str, details: str):
        if not self.enabled or not self.notify_trades:
            return
        self._send_message(
            f"\U0001f504 <b>MULTI-TF BLOCAGE</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{symbol}: {details}\n"
            f"\U0001f6ab Signal non confirme sur les timeframes superieurs."
        )

    def get_session_summary(self) -> str:
        """Retourne un resume de la session en cours."""
        wr = (self._win_count / self._trade_count * 100) if self._trade_count > 0 else 0
        return (
            f"Session: {self._trade_count} trades | "
            f"{self._win_count}W/{self._loss_count}L | "
            f"WR: {wr:.0f}% | "
            f"PnL: {self._daily_pnl:+.2f}$ | "
            f"Total: {self._total_pnl:+.2f}$ | "
            f"Streak: {self._current_streak:+d} | "
            f"Best: {self._best_streak} | "
            f"Worst: {abs(self._worst_streak)}"
        )
