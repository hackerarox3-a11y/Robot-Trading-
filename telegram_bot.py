"""
Serveur de commandes Telegram pour le robot de trading.
=======================================================
Permet de controler le robot directement depuis Telegram :
  - /start        : Demarre le bot (affiche le menu avec boutons)
  - /status       : Etat actuel (broker, capital, positions)
  - /switch deriv : Change vers Deriv
  - /switch mt5   : Change vers Exness (MT5)
  - /switch both  : Utilise les deux brokers
  - /balance      : Solde des comptes connectes
  - /positions    : Positions ouvertes
  - /symbols      : Symboles surveilles
  - /stop         : Arrete le robot
  - /pause        : Pause les trades (le bot continue de surveiller)
  - /resume       : Reprend les trades
  - /pnl          : Resume PnL de la session
  - /config       : Affiche la config active
  - /trades [N]   : Derniers N trades depuis le CSV (defaut 10)
  - /risk         : Score de risque et etat du risk manager
  - /compound     : Niveau de croissance composee et progression
  - /performance  : Statistiques detaillees de la session
   - /clear        : Remet a zero les stats quotidiennes
  - /set cle val  : Modifie une valeur de config a chaud
  - /restart      : Redemarre le robot
  - /help         : Aide complete

Le bot ecoute en arriere-plan dans un thread separe.
Le switch de broker est effectue en temps reel sans redemarrage.

Securite :
  - Seuls les chat_ids autorises peuvent envoyer des commandes
  - (optionnel) Mot de passe pour les commandes sensibles (/stop, /switch, /restart)

Ameliorations v4 :
  - Boutons inline (clavier) pour actions rapides
  - Commandes /trades, /risk, /compound, /performance, /clear, /set, /restart
  - Nettoyage automatique des mises a jour traitees
  - Suivi de debut/fin de session
  - Formatage ameliore des messages
"""

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import csv
from datetime import datetime
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TelegramCommandBot:
    """Bot Telegram qui ecoute les commandes en arriere-plan avec boutons inline."""

    TELEGRAM_API = "https://api.telegram.org/bot{token}"
    POLL_INTERVAL = 2  # secondes entre chaque poll
    MAX_PROCESSED_UPDATES = 500  # nombre max d'updates processees avant nettoyage

    def __init__(self, config: dict):
        self.config = config
        tg = config.get("telegram", {})
        self.enabled = tg.get("enabled", False)
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", str(tg.get("chat_id", "")))
        self.allowed_chat_ids = [self.chat_id]
        # Ajouter d'autres chat_ids autorises si configures
        extra = tg.get("allowed_chat_ids", [])
        if extra:
            self.allowed_chat_ids.extend([str(c) for c in extra])
        self.admin_password = os.getenv("TELEGRAM_ADMIN_PASSWORD", tg.get("admin_password", ""))
        self._offset = 0
        self._running = False
        self._thread = None
        self._processed_count = 0
        self._session_start = datetime.now()
        self._session_end = None
        # Callbacks - seront connectes au TradingBot
        self._on_switch_broker: Optional[Callable] = None
        self._on_stop: Optional[Callable] = None
        self._on_pause: Optional[Callable] = None
        self._on_resume: Optional[Callable] = None
        self._on_get_status: Optional[Callable] = None
        self._on_get_balance: Optional[Callable] = None
        self._on_get_positions: Optional[Callable] = None
        # Callbacks etendus (v4)
        self._on_get_trades: Optional[Callable] = None
        self._on_get_risk: Optional[Callable] = None
        self._on_get_compound: Optional[Callable] = None
        self._on_get_performance: Optional[Callable] = None
        self._on_clear_stats: Optional[Callable] = None
        self._on_set_config: Optional[Callable] = None
        self._on_restart: Optional[Callable] = None
        # Chemin du CSV de trades
        self._trades_csv = config.get("logging", {}).get("trades_csv_file", "trades_history.csv")

    def connect_callbacks(self, switch_fn, stop_fn, pause_fn, resume_fn,
                          status_fn, balance_fn, positions_fn):
        """Connecte les callbacks principaux vers le TradingBot."""
        self._on_switch_broker = switch_fn
        self._on_stop = stop_fn
        self._on_pause = pause_fn
        self._on_resume = resume_fn
        self._on_get_status = status_fn
        self._on_get_balance = balance_fn
        self._on_get_positions = positions_fn

    def connect_extended_callbacks(self, trades_fn=None, risk_fn=None, compound_fn=None,
                                    performance_fn=None, clear_fn=None, set_fn=None,
                                    restart_fn=None):
        """Connecte les callbacks etendus (v4)."""
        if trades_fn:
            self._on_get_trades = trades_fn
        if risk_fn:
            self._on_get_risk = risk_fn
        if compound_fn:
            self._on_get_compound = compound_fn
        if performance_fn:
            self._on_get_performance = performance_fn
        if clear_fn:
            self._on_clear_stats = clear_fn
        if set_fn:
            self._on_set_config = set_fn
        if restart_fn:
            self._on_restart = restart_fn

    def start(self):
        """Demarre le listener dans un thread separe."""
        if not self.enabled:
            logger.info("Commandes Telegram desactivees.")
            return
        if not self.token or self.token == "VOTRE_TOKEN_TELEGRAM_ICI":
            logger.info("Commandes Telegram: token non configure.")
            self.enabled = False
            return
        if not self._validate_token():
            self.enabled = False
            return
        self._running = True
        self._session_start = datetime.now()
        self._session_end = None
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="TG-CmdBot")
        self._thread.start()
        logger.info("Serveur de commandes Telegram demarre.")

    def _validate_token(self) -> bool:
        """Verifie le token via Telegram avant de lancer le polling."""
        url = f"{self.TELEGRAM_API.format(token=self.token)}/getMe"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("ok"):
                bot = data.get("result", {})
                logger.info("Token Telegram valide: @%s", bot.get("username", "inconnu"))
                return True
            description = data.get("description", "reponse Telegram invalide")
            logger.error("Token Telegram invalide: %s", description)
        except urllib.error.HTTPError as error:
            logger.error("Token Telegram invalide (HTTP %s). Verifie TELEGRAM_BOT_TOKEN.", error.code)
        except (urllib.error.URLError, TimeoutError) as error:
            logger.error("Telegram inaccessible pendant la validation du token: %s", error)
        except (ValueError, KeyError) as error:
            logger.error("Reponse Telegram inattendue pendant la validation: %s", error)
        return False

    def stop(self):
        """Arrete le listener."""
        self._running = False
        self._session_end = datetime.now()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Serveur de commandes Telegram arrete.")

    def _poll_loop(self):
        """Boucle de polling des mises a jour Telegram."""
        while self._running:
            try:
                self._get_updates()
            except Exception as e:
                logger.debug(f"Telegram poll erreur: {e}")
            time.sleep(self.POLL_INTERVAL)

    def _get_updates(self):
        """Recupere les nouvelles mises a jour (messages + callback_query)."""
        url = f"{self.TELEGRAM_API.format(token=self.token)}/getUpdates"
        params = urllib.parse.urlencode({
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        })
        req = urllib.request.Request(f"{url}?{params}")
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode())
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            self._processed_count += 1

            # Traiter les callback_query (boutons inline)
            callback_query = update.get("callback_query")
            if callback_query:
                self._handle_callback_query(callback_query)
                continue

            msg = update.get("message", {})
            if not msg:
                continue
            chat_id = str(msg["chat"]["id"])
            text = msg.get("text", "")
            if not text or not text.startswith("/"):
                continue
            # Verifier autorisation
            if chat_id not in self.allowed_chat_ids:
                self._send_message(chat_id,
                    "\u26a0\ufe0f <b>NON AUTORISE</b>\n"
                    "Votre chat_id n'est pas autorise."
                )
                continue
            self._handle_command(chat_id, text.strip())

        # Nettoyage automatique des updates traitees
        if self._processed_count >= self.MAX_PROCESSED_UPDATES:
            self._confirm_and_cleanup()
            self._processed_count = 0

    def _confirm_and_cleanup(self):
        """Confirme toutes les mises a jour comme traitees pour liberer la file."""
        try:
            url = f"{self.TELEGRAM_API.format(token=self.token)}/getUpdates"
            params = urllib.parse.urlencode({
                "offset": self._offset,
                "timeout": 1,
            })
            req = urllib.request.Request(f"{url}?{params}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
            logger.debug("Nettoyage des updates Telegram effectue.")
        except Exception as e:
            logger.debug(f"Nettoyage updates echoue: {e}")

    def _handle_callback_query(self, callback_query: dict):
        """Traite les appuis sur les boutons inline."""
        query_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        msg = callback_query.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id not in self.allowed_chat_ids:
            return

        # Repondre au callback pour enlever le sablier
        self._answer_callback_query(query_id)

        if data == "switch_deriv":
            self._cmd_switch(chat_id, [])
        elif data == "switch_mt5":
            self._cmd_switch(chat_id, [])
        elif data == "switch_both":
            self._cmd_switch(chat_id, [])
        elif data == "pause_bot":
            self._cmd_pause(chat_id, [])
        elif data == "resume_bot":
            self._cmd_resume(chat_id, [])
        elif data == "show_status":
            self._cmd_status(chat_id)
        elif data == "show_balance":
            self._cmd_balance(chat_id)
        elif data == "show_positions":
            self._cmd_positions(chat_id)
        elif data == "show_performance":
            self._cmd_performance(chat_id)
        elif data == "show_risk":
            self._cmd_risk(chat_id)
        elif data == "show_compound":
            self._cmd_compound(chat_id)
        elif data == "show_trades":
            self._cmd_trades(chat_id, [])
        elif data.startswith("switch_"):
            # Gestion generique des switchs via boutons
            target = data.replace("switch_", "")
            if target in ("deriv", "mt5", "both"):
                self._cmd_switch(chat_id, [])

    def _answer_callback_query(self, query_id: str, text: str = ""):
        """Repond a un callback_query pour enlever le sablier sur le bouton."""
        try:
            url = f"{self.TELEGRAM_API.format(token=self.token)}/answerCallbackQuery"
            payload = {"callback_query_id": query_id}
            if text:
                payload["text"] = text
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                                          headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as e:
            logger.debug(f"Reponse callback echouee: {e}")

    def _handle_command(self, chat_id: str, text: str):
        """Traite une commande recue."""
        parts = text.split(maxsplit=3)
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        # Commandes sans mot de passe
        if cmd == "/start":
            return self._cmd_start(chat_id)
        elif cmd == "/help":
            return self._cmd_help(chat_id)
        elif cmd == "/status":
            return self._cmd_status(chat_id)
        elif cmd == "/balance":
            return self._cmd_balance(chat_id)
        elif cmd == "/positions":
            return self._cmd_positions(chat_id)
        elif cmd == "/symbols":
            return self._cmd_symbols(chat_id)
        elif cmd == "/pnl":
            return self._cmd_pnl(chat_id)
        elif cmd == "/config":
            return self._cmd_config(chat_id)
        elif cmd == "/trades":
            return self._cmd_trades(chat_id, args)
        elif cmd == "/risk":
            return self._cmd_risk(chat_id)
        elif cmd == "/compound":
            return self._cmd_compound(chat_id)
        elif cmd == "/performance":
            return self._cmd_performance(chat_id)

        # Commandes protegees par mot de passe
        if cmd == "/switch":
            return self._cmd_switch(chat_id, args)
        elif cmd == "/stop":
            return self._cmd_stop(chat_id, args)
        elif cmd == "/pause":
            return self._cmd_pause(chat_id, args)
        elif cmd == "/resume":
            return self._cmd_resume(chat_id, args)
        elif cmd == "/clear":
            return self._cmd_clear(chat_id, args)
        elif cmd == "/set":
            return self._cmd_set(chat_id, args)
        elif cmd == "/restart":
            return self._cmd_restart(chat_id, args)

        self._send_message(chat_id, "\u2753 Commande inconnue. /help pour voir les commandes.")

    def _check_password(self, chat_id: str, args: list) -> bool:
        """Verifie le mot de passe si configure."""
        if not self.admin_password:
            return True
        if not args or args[0] != self.admin_password:
            self._send_message(chat_id,
                "\U0001f512 <b>MOT DE PASSE REQUIS</b>\n"
                "Usage: /commande <mot_de_passe> [args]"
            )
            return False
        return True

    # ------------------------------------------------------------------
    #  CLAVIER INLINE
    # ------------------------------------------------------------------

    def _build_main_keyboard(self) -> str:
        """Construit le clavier inline principal en JSON."""
        keyboard = [
            [{"text": "\U0001f4ca Statut", "callback_data": "show_status"},
             {"text": "\U0001f4b0 Solde", "callback_data": "show_balance"},
             {"text": "\U0001f4c8 Positions", "callback_data": "show_positions"}],
            [{"text": "\U0001f3af Performance", "callback_data": "show_performance"},
             {"text": "\u26a0\ufe0f Risque", "callback_data": "show_risk"},
             {"text": "\U0001f3c6 Croissance", "callback_data": "show_compound"}],
            [{"text": "\U0001f4dc Derniers trades", "callback_data": "show_trades"}],
            [{"text": "\u2b05\ufe0f Switch Deriv", "callback_data": "switch_deriv"},
             {"text": "\u2b05\ufe0f Switch MT5", "callback_data": "switch_mt5"},
             {"text": "\u27a1\ufe0f Les deux", "callback_data": "switch_both"}],
            [{"text": "\u23f8\ufe0f Pause", "callback_data": "pause_bot"},
             {"text": "\u25b6\ufe0f Reprendre", "callback_data": "resume_bot"}],
        ]
        return json.dumps({"inline_keyboard": keyboard})

    # ------------------------------------------------------------------
    #  COMMANDES
    # ------------------------------------------------------------------

    def _cmd_start(self, chat_id: str):
        """Message de bienvenue avec clavier inline."""
        uptime = str(datetime.now() - self._session_start).split(".")[0] if self._session_start else "N/A"
        self._send_message(chat_id,
            "\U0001f916 <b>ROBOT TRADING v4</b>\n"
            "\n"
            "\U0001f310 <b>Brokers disponibles :</b>\n"
            "  \u2022 Deriv (indices synthetiques)\n"
            "  \u2022 Exness / MT5 (Forex, Or)\n"
            "  \u2022 Les deux simultanement\n"
            "\n"
            "\U0001f4e1 <b>Commandes principales :</b>\n"
            "  /status - Etat du robot\n"
            "  /trades - Derniers trades\n"
            "  /risk - Score de risque\n"
            "  /compound - Croissance composee\n"
            "  /performance - Stats detaillees\n"
            "  /set cle val - Config a chaud\n"
            "\n"
            "\U0001f4c4 Utilise les boutons ci-dessous ou /help"
            "\n"
            f"\U0001f552 Session demarree : {self._session_start.strftime('%d/%m/%Y %H:%M')}\n"
            f"\u23f1 Uptime : {uptime}",
            reply_markup=self._build_main_keyboard()
        )

    def _cmd_help(self, chat_id: str):
        pwd_note = ""
        if self.admin_password:
            pwd_note = ("\n\U0001f512 Les commandes /switch, /stop, /pause, /resume, "
                        "/clear, /set, /restart necessitent le mot de passe.")
        self._send_message(chat_id,
            "\U0001f4cb <b>AIDE COMPLETE v4</b>\n"
            "\n"
            "\U0001f50d <b>Information :</b>\n"
            "  /status    - Etat du robot (broker, mode, capital)\n"
            "  /balance   - Solde detaille par broker\n"
            "  /positions - Positions ouvertes en cours\n"
            "  /symbols   - Symboles surveilles\n"
            "  /pnl       - Resume PnL de la session\n"
            "  /config    - Configuration active\n"
            "\n"
            "\U0001f4ca <b>Analyse :</b>\n"
            "  /trades [N]   - Derniers N trades du CSV (defaut 10)\n"
            "  /risk         - Score de risque et etat\n"
            "  /compound     - Niveau de croissance composee\n"
            "  /performance  - Statistiques detaillees de session\n"
            "\n"
            "\U0001f527 <b>Controle :</b>\n"
            "  /switch deriv  - Passer a Deriv uniquement\n"
            "  /switch mt5   - Passer a Exness (MT5) uniquement\n"
            "  /switch both  - Utiliser les deux brokers\n"
            "  /pause [pwd]  - Pause les trades\n"
            "  /resume [pwd] - Reprendre les trades\n"
            "  /stop [pwd]   - Arreter completement le robot\n"
            "  /restart [pwd]- Redemarrer le robot\n"
            "\n"
            "\U0001f527 <b>Configuration :</b>\n"
            "  /set cle val  - Modifier une valeur de config\n"
            "  /clear [pwd]  - Remettre a zero les stats du jour\n"
            "\n"
            "\U0001f4c1 <b>Brokers :</b>\n"
            "  \u2022 <b>Deriv</b> : R_75, R_100, BOOM1000, CRASH1000, frxEURUSD\n"
            "  \u2022 <b>Exness</b> : XAUUSD, EURUSD, GBPUSD, USDJPY, USDCHF"
            f"{pwd_note}"
        )

    def _cmd_status(self, chat_id: str):
        if self._on_get_status:
            status = self._on_get_status()
            self._send_message(chat_id, status)
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_balance(self, chat_id: str):
        if self._on_get_balance:
            balance_msg = self._on_get_balance()
            self._send_message(chat_id, balance_msg)
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_positions(self, chat_id: str):
        if self._on_get_positions:
            pos_msg = self._on_get_positions()
            self._send_message(chat_id, pos_msg)
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_symbols(self, chat_id: str):
        symbols = self.config.get("trading", {}).get("symbols", [])
        deriv_symbols = self.config.get("brokers", {}).get("deriv", {}).get("symbols", [])
        mt5_symbols = self.config.get("brokers", {}).get("mt5", {}).get("symbols", [])
        text = "\U0001f4c8 <b>SYMBOLS SURVEILLES</b>\n\n"
        if deriv_symbols:
            text += "\U0001f535 <b>Deriv :</b>\n"
            for s in deriv_symbols:
                text += f"  \u2022 {s}\n"
            text += f"  <i>({len(deriv_symbols)} symbole(s))</i>\n"
        if mt5_symbols:
            text += "\n\U0001f7e2 <b>Exness (MT5) :</b>\n"
            for s in mt5_symbols:
                text += f"  \u2022 {s}\n"
            text += f"  <i>({len(mt5_symbols)} symbole(s))</i>\n"
        if not deriv_symbols and not mt5_symbols:
            for s in symbols:
                text += f"  \u2022 {s}\n"
            text += f"  <i>({len(symbols)} symbole(s))</i>\n"
        self._send_message(chat_id, text)

    def _cmd_pnl(self, chat_id: str):
        if self._on_get_status:
            status = self._on_get_status()
            self._send_message(chat_id, status)
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_config(self, chat_id: str):
        cfg = self.config
        broker = cfg.get("active_broker", cfg.get("broker", "deriv"))
        broker_label = {"deriv": "Deriv", "mt5": "Exness", "both": "Les deux"}.get(broker, broker)
        tf = cfg.get("trading", {}).get("timeframe", "M5")
        scan = cfg.get("timing", {}).get("scan_interval_seconds", 30)
        compound = cfg.get("compound_growth", {}).get("enabled", False)
        mtf = cfg.get("multi_timeframe", {}).get("enabled", False)
        news = cfg.get("news_filter", {}).get("enabled", False)
        auto = cfg.get("auto_market_select", {}).get("enabled", True)
        session = cfg.get("session_filter", {})
        avoid_wknd = session.get("avoid_weekend", True)
        fri_close = session.get("friday_close_hour", 20)
        self._send_message(chat_id,
            f"\u2699\ufe0f <b>CONFIGURATION ACTIVE</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f310 Broker actif : <b>{broker_label}</b>\n"
            f"\U0001f552 Timeframe : <b>{tf}</b>\n"
            f"\U0001f504 Intervalle scan : <b>{scan}s</b>\n"
            f"\U0001f3af Selection auto marche : {'\u2705 ON' if auto else '\u274c OFF'}\n"
            f"\U0001f525 Croissance composee : {'\u2705 ON' if compound else '\u274c OFF'}\n"
            f"\U0001f504 Multi-Timeframe : {'\u2705 ON' if mtf else '\u274c OFF'}\n"
            f"\U0001f4f0 Filtre News : {'\u2705 ON' if news else '\u274c OFF'}\n"
            f"\U0001f4c5 Eviter weekend : {'\u2705 Oui' if avoid_wknd else '\u274c Non'}\n"
            f"\U0001f319 Fermeture vendredi : <b>{fri_close}h</b>\n"
            f"\U0001f4c5 Fichier config : <code>{os.path.abspath(self.config.get('_path', 'config.json'))}</code>"
        )

    def _cmd_trades(self, chat_id: str, args: list):
        """Affiche les derniers N trades depuis le fichier CSV."""
        n = 10
        if args and args[0].isdigit():
            n = min(int(args[0]), 50)

        # Verifier d'abord si un callback est connecte
        if self._on_get_trades:
            result = self._on_get_trades(n)
            self._send_message(chat_id, result)
            return

        # Sinon, lire directement le CSV
        if not os.path.exists(self._trades_csv):
            self._send_message(chat_id,
                f"\U0001f4c4 <b>DERNIERS {n} TRADES</b>\n\n"
                f"\u26a0\ufe0f Fichier <code>{self._trades_csv}</code> introuvable."
            )
            return

        try:
            with open(self._trades_csv, "r", encoding="utf-8") as f:
                reader = list(csv.reader(f))
            if len(reader) <= 1:
                self._send_message(chat_id,
                    f"\U0001f4c4 <b>DERNIERS {n} TRADES</b>\n\n"
                    "Aucun trade enregistre."
                )
                return
            headers = reader[0]
            rows = reader[1:]
            # Prendre les N derniers
            last_rows = rows[-n:]
            total = len(rows)

            text = f"\U0001f4c4 <b>DERNIERS {len(last_rows)} TRADES</b> (sur {total})\n"
            text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"

            for row in reversed(last_rows):
                row_dict = dict(zip(headers, row)) if len(row) == len(headers) else {}
                ts = row_dict.get("timestamp", row[0] if row else "?")
                symbol = row_dict.get("symbol", row[2] if len(row) > 2 else "?")
                direction = row_dict.get("direction", row[3] if len(row) > 3 else "?")
                pnl_str = row_dict.get("pnl", row[9] if len(row) > 9 else "")
                broker = row_dict.get("broker", row[1] if len(row) > 1 else "?")
                reason = row_dict.get("reason", row[11] if len(row) > 11 else "")

                try:
                    pnl_val = float(pnl_str) if pnl_str else 0
                except (ValueError, TypeError):
                    pnl_val = 0

                if pnl_val > 0:
                    pnl_emoji = "\u2705"
                    pnl_fmt = f"<b>+{pnl_val:.2f}$</b>"
                elif pnl_val < 0:
                    pnl_emoji = "\u274c"
                    pnl_fmt = f"<b>{pnl_val:.2f}$</b>"
                else:
                    pnl_emoji = "\u23f8\ufe0f"
                    pnl_fmt = "en cours"

                dir_emoji = "\U0001f7e2" if direction.upper() == "BUY" else "\U0001f534" if direction.upper() == "SELL" else "\u2753"
                broker_tag = f"[{broker.upper()}] " if broker else ""

                text += (
                    f"{pnl_emoji} {broker_tag}{dir_emoji} <b>{symbol}</b> {direction}\n"
                    f"  PnL: {pnl_fmt} | {ts}\n"
                )
                if reason:
                    text += f"  Raison: {reason}\n"
                text += "\n"

            self._send_message(chat_id, text)
        except Exception as e:
            self._send_message(chat_id,
                f"\U0001f4c4 <b>ERREUR LECTURE TRADES</b>\n\n"
                f"{str(e)}"
            )

    def _cmd_risk(self, chat_id: str):
        """Affiche le score de risque et l'etat du risk manager."""
        if self._on_get_risk:
            result = self._on_get_risk()
            self._send_message(chat_id, result)
        else:
            self._send_message(chat_id,
                "\u26a0\ufe0f <b>SCORE DE RISQUE</b>\n\n"
                "Callback non connecte. Le module de gestion du risque n'est pas encore initialise."
            )

    def _cmd_compound(self, chat_id: str):
        """Affiche le niveau de croissance composee et la progression."""
        if self._on_get_compound:
            result = self._on_get_compound()
            self._send_message(chat_id, result)
        else:
            self._send_message(chat_id,
                "\u26a0\ufe0f <b>CROISSANCE COMPOSEE</b>\n\n"
                "Callback non connecte. Le module de croissance composee n'est pas encore initialise."
            )

    def _cmd_performance(self, chat_id: str):
        """Affiche les statistiques detaillees de la session."""
        if self._on_get_performance:
            result = self._on_get_performance()
            self._send_message(chat_id, result)
        else:
            self._send_message(chat_id,
                "\u26a0\ufe0f <b>PERFORMANCE</b>\n\n"
                "Callback non connecte. Les statistiques ne sont pas encore disponibles."
            )

    def _cmd_clear(self, chat_id: str, args: list):
        """Remet a zero les statistiques quotidiennes."""
        if not self._check_password(chat_id, args):
            return
        if self._on_clear_stats:
            self._on_clear_stats()
            self._send_message(chat_id,
                "\U0001f5d1\ufe0f <b>STATS REMISES A ZERO</b>\n\n"
                "Les statistiques quotidiennes ont ete reinitialisees.\n"
                f"\U0001f552 {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_set(self, chat_id: str, args: list):
        """Modifie une valeur de configuration a chaud."""
        if not self._check_password(chat_id, args):
            return
        # args[0] = mot de passe, args[1] = cle, args[2] = valeur
        if len(args) < 2:
            self._send_message(chat_id,
                "\u2753 <b>Usage :</b> /set [mot_de_passe] <cle> <valeur>\n\n"
                "<b>Cles possibles :</b>\n"
                "  scan_interval - Intervalle de scan (secondes)\n"
                "  mode - Mode de trading (normal/strict/conservateur)\n"
                "  max_positions - Nombre max de positions\n"
                "  default_lot - Mise par defaut\n"
                "  Exemple: /set pwd scan_interval 60"
            )
            return

        key = args[0].lower() if not self.admin_password else (args[1].lower() if len(args) > 1 else "")
        value = args[1] if not self.admin_password else (args[2] if len(args) > 2 else "")

        if not key or not value:
            self._send_message(chat_id, "\u2753 <b>Usage :</b> /set [mot_de_passe] <cle> <valeur>")
            return

        if self._on_set_config:
            result = self._on_set_config(key, value)
            self._send_message(chat_id, result)
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_restart(self, chat_id: str, args: list):
        """Redemarre le robot."""
        if not self._check_password(chat_id, args):
            return
        self._send_message(chat_id,
            "\U0001f504 <b>REDEMARRAGE EN COURS...</b>\n"
            "Sauvegarde de l'etat et redemarrage du robot.\n"
            "Cela peut prendre quelques secondes..."
        )
        if self._on_restart:
            self._on_restart()
        else:
            # Fallback: arret puis indication
            if self._on_stop:
                self._on_stop()
            self._send_message(chat_id,
                "\u26a0\ufe0f Callback de redemarrage non connecte.\n"
                "Le bot a ete arrete. Relancez-le manuellement."
            )

    def _cmd_switch(self, chat_id: str, args: list):
        if not self._check_password(chat_id, args):
            return
        # Si appele via bouton inline, args peut etre vide
        # Dans ce cas, on ne peut pas deviner le broker cible
        target = ""
        if self.admin_password and len(args) > 1:
            target = args[1]
        elif not self.admin_password and len(args) > 0:
            target = args[0]
        target = target.lower().strip()
        if target not in ("deriv", "mt5", "both"):
            self._send_message(chat_id,
                "\u2753 <b>Usage :</b> /switch [mot_de_passe] <broker>\n\n"
                "Brokers disponibles :\n"
                "  /switch deriv  - Indices synthetiques\n"
                "  /switch mt5    - Forex & Or via Exness\n"
                "  /switch both   - Les deux brokers\n\n"
                "\U0001f4a1 Ou utilise les boutons ci-dessous :"
            )
            return
        if self._on_switch_broker:
            result = self._on_switch_broker(target)
            self._send_message(chat_id, result, reply_markup=self._build_main_keyboard())
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_stop(self, chat_id: str, args: list):
        if not self._check_password(chat_id, args):
            return
        self._send_message(chat_id,
            "\U0001f6d1 <b>ARRET EN COURS...</b>\n"
            "Fermeture des positions et arret du robot.\n"
            f"\U0001f552 Session : {self._session_start.strftime('%d/%m/%Y %H:%M')} -> {datetime.now().strftime('%H:%M')}"
        )
        if self._on_stop:
            self._on_stop()

    def _cmd_pause(self, chat_id: str, args: list):
        if not self._check_password(chat_id, args):
            return
        if self._on_pause:
            self._send_message(chat_id,
                "\u23f8\ufe0f <b>PAUSE ACTIVEE</b>\n"
                "Le bot continue de surveiller les marches mais n'ouvre plus de positions.\n"
                "Toutes les positions ouvertes restent actives.\n"
                "/resume pour reprendre le trading."
            )
            self._on_pause()
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    def _cmd_resume(self, chat_id: str, args: list):
        if not self._check_password(chat_id, args):
            return
        if self._on_resume:
            self._send_message(chat_id,
                "\u25b6\ufe0f <b>REPRISE DU TRADING</b>\n"
                "Le bot reprend le trading normalement.\n"
                "Analyse des marches et ouverture de positions reactivee."
            )
            self._on_resume()
        else:
            self._send_message(chat_id, "\u26a0\ufe0f Callback non connecte.")

    # ------------------------------------------------------------------
    #  ENVOI
    # ------------------------------------------------------------------

    def _send_message(self, chat_id: str, text: str, parse_mode: str = "HTML",
                       reply_markup: str = None):
        url = f"{self.TELEGRAM_API.format(token=self.token)}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if not result.get("ok"):
                    logger.debug(f"Telegram send error: {result}")
        except Exception as e:
            logger.debug(f"Telegram send echoue: {e}")

    def notify_startup(self, active_broker: str):
        """Envoie une notification de demarrage avec le broker actif."""
        if not self.enabled:
            return
        broker_emoji = {"deriv": "\U0001f535", "mt5": "\U0001f7e2", "both": "\U0001f535\U0001f7e2"}
        emoji = broker_emoji.get(active_broker, "\U0001f310")
        label = {"deriv": "Deriv", "mt5": "Exness", "both": "Deriv + Exness"}.get(active_broker, active_broker)
        mode_label = "DRY-RUN" if self.config.get("_dry_run", False) else "LIVE"
        self._send_message(self.chat_id,
            f"{emoji} <b>ROBOT TRADING v4 DEMARRE</b>\n"
            f"\U0001f310 Broker actif : <b>{label}</b>\n"
            f"\U0001f3ae Mode : <b>{mode_label}</b>\n"
            f"\U0001f552 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
            f"\U0001f4e1 Utilise /help pour les commandes\n"
            f"\U0001f504 /switch deriv ou /switch mt5 pour changer\n\n"
            f"\U0001f4c4 Ou utilise les boutons ci-dessous :",
            reply_markup=self._build_main_keyboard()
        )

    def notify_shutdown(self):
        """Envoie une notification d'arret avec resume de session."""
        if not self.enabled:
            return
        self._session_end = datetime.now()
        duration = str(self._session_end - self._session_start).split(".")[0]
        self._send_message(self.chat_id,
            f"\U0001f6d1 <b>ROBOT ARRETE</b>\n"
            f"\U0001f552 Session : {self._session_start.strftime('%d/%m/%Y %H:%M')} - {self._session_end.strftime('%H:%M')}\n"
            f"\u23f1 Duree : {duration}\n"
            f"\U0001f4ca Cycles effectues : {self._processed_count} updates traitees"
        )

    def get_session_duration(self) -> str:
        """Retourne la duree de la session en cours."""
        end = self._session_end or datetime.now()
        return str(end - self._session_start).split(".")[0]
