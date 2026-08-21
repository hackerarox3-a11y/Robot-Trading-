# -*- coding: utf-8 -*-
"""
Robot de Trading Multi-Broker v4 - Controle Telegram Avance
========================================================
Supporte MT5 (Exness) ET Deriv (indices synthetiques) simultanement.
Switch de broker en temps reel via Telegram ou SIGHUP.

Fonctionnalites v4 :
  1. Mode "both" : trade sur Deriv ET Exness en meme temps
  2. Controle Telegram complet (toutes les commandes + boutons inline)
  3. Switch broker en temps reel sans redemarrage (avec sauvegarde d'etat)
  4. Symboles separes par broker dans config
  5. Notifications Telegram avancees (risque, compose, mode, connexion)
  6. Thread de sante (health check) toutes les 60s
  7. Degradation gracieuse (si un broker echoue, continuer avec l'autre)
  8. Auto-sauvegarde de l'etat toutes les 5 minutes
  9. Diagnostics de demarrage complets
 10. Metriques de performance (latence, taux de succes par broker)
 11. Intervalle de scan specifique par symbole (volatilite)
 12. Arret d'urgence si perte totale depasse un seuil
 13. Rechargement a chaud de la config (/reload, SIGHUP)
 14. Mode dry-run ameliore (simulation avec rapports)
 15. Recuperation d'erreur amelioree par cycle

Utilisation :
    py main.py                     # Demarrage normal (broker du config)
    py main.py --broker both       # Les deux brokers
    py main.py --broker deriv      # Force Deriv
    py main.py --broker mt5        # Force MT5
    py main.py --dry-run           # Mode simulation
    py main.py --backtest          # Lance le backtest
    py main.py --backtest --report html  # Backtest avec rapport HTML
"""

import json
import time
import logging
import argparse
import os
import csv
import signal as sig
import sys
import copy
import threading
import traceback
from datetime import datetime
from typing import Dict, List, Optional

from technical_analysis import TechnicalAnalysis
from risk_manager import RiskManager
from strategy_engine import StrategyEngine
from market_selector import MarketSelector
from compound_manager import CompoundManager
from multi_timeframe import MultiTimeframeAnalyzer
from news_filter import NewsFilter
from telegram_notifier import TelegramNotifier
from telegram_bot import TelegramCommandBot
from deriv_connector import DerivConnector, ORDER_TYPE_BUY as DERIV_BUY, ORDER_TYPE_SELL as DERIV_SELL

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    logging.getLogger(__name__).warning(
        "python-dotenv non installe: le fichier .env ne sera pas charge."
    )

# MT5 est optionnel (seulement sur Windows avec MetaTrader 5 installe)
try:
    from mt5_connector import MT5Connector, ORDER_TYPE_BUY as MT5_BUY, ORDER_TYPE_SELL as MT5_SELL
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    MT5_BUY = DERIV_BUY
    MT5_SELL = DERIV_SELL


# Mapping des types d'ordres par broker
ORDER_TYPES = {
    "deriv": {"buy": DERIV_BUY, "sell": DERIV_SELL},
    "mt5": {"buy": MT5_BUY, "sell": MT5_SELL},
}

# Symboles consideres comme volatils (scan plus frequent)
VOLATILE_SYMBOLS = {"BOOM1000", "BOOM500", "CRASH1000", "CRASH500", "XAUUSD", "XAGUSD"}

logger = logging.getLogger(__name__)


def setup_logging(config: dict):
    log_cfg = config["logging"]
    log_level = getattr(logging, log_cfg.get("level", "INFO"))
    log_format = "%(asctime)s [%(levelname)-7s] %(name)-25s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    handlers = [logging.StreamHandler()]
    if log_cfg.get("log_to_file", True):
        log_file = log_cfg.get("log_file", "trading_bot.log")
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=log_level, format=log_format, datefmt=date_format, handlers=handlers)


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


class TradingBot:
    """Robot de trading multi-broker avec controle Telegram avance."""

    def __init__(self, config_path: str, dry_run: bool = False, broker: str = None):
        self.dry_run = dry_run
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.config["_path"] = config_path
        self.config["_dry_run"] = dry_run
        self.running = False
        self.paused = False
        self.scan_interval = self.config["timing"]["scan_interval_seconds"]
        self.cycle_count = 0

        # --- Broker actif : "deriv", "mt5", ou "both" ---
        self.active_broker = broker or self.config.get("active_broker", self.config.get("broker", "both"))
        if self.active_broker not in ("deriv", "mt5", "both"):
            self.active_broker = "both"

        # --- Connecteurs ---
        self.connectors: Dict[str, object] = {}
        self._init_connectors()

        # --- Analyse technique et strategies ---
        self.technical = TechnicalAnalysis(self.config)
        self.strategy = StrategyEngine(self.config)
        self.risk_managers: Dict[str, RiskManager] = {}
        self.market_selector = MarketSelector(self.config)
        self.compound_managers: Dict[str, CompoundManager] = {}
        self.auto_select = self.config.get("auto_market_select", {}).get("enabled", True)
        self.trades_csv = self.config["logging"].get("trades_csv_file", "trades_history.csv")
        self._init_trades_csv()
        self._positions_snapshots: Dict[str, dict] = {}
        self._symbol_engines: Dict[str, dict] = {}
        self._build_symbol_engines()

        # --- Modules v2 ---
        self.mtf = MultiTimeframeAnalyzer(self.config)
        self.news_filter = NewsFilter(self.config)
        self.telegram = TelegramNotifier(self.config)

        # --- Telegram Command Bot (v4) ---
        self.cmd_bot = TelegramCommandBot(self.config)
        self.cmd_bot.connect_callbacks(
            switch_fn=self._telegram_switch_broker,
            stop_fn=self._telegram_stop,
            pause_fn=self._telegram_pause,
            resume_fn=self._telegram_resume,
            status_fn=self._telegram_status,
            balance_fn=self._telegram_balance,
            positions_fn=self._telegram_positions,
        )
        self.cmd_bot.connect_extended_callbacks(
            trades_fn=self._telegram_trades,
            risk_fn=self._telegram_risk,
            compound_fn=self._telegram_compound,
            performance_fn=self._telegram_performance,
            clear_fn=self._telegram_clear,
            set_fn=self._telegram_set_config,
            restart_fn=self._telegram_restart,
        )

        # Tracking pour Telegram
        self._trade_open_times: Dict[int, datetime] = {}

        # --- v4: Metriques de performance ---
        self._broker_metrics: Dict[str, dict] = {}
        for bkr in ("deriv", "mt5"):
            self._broker_metrics[bkr] = {
                "total_requests": 0,
                "success_count": 0,
                "error_count": 0,
                "total_latency_ms": 0.0,
                "last_error": None,
                "last_error_time": None,
                "last_success_time": None,
            }

        # --- v4: Etat pour sauvegarde ---
        self._state_file = ".bot_state.json"
        self._last_auto_save = time.time()
        self._auto_save_interval = 300  # 5 minutes

        # --- v4: Seuil de perte d'urgence ---
        self._emergency_loss_threshold = self.config.get("risk_management", {}).get(
            "emergency_loss_threshold", -50.0
        )
        self._initial_total_balance = 0.0

        # --- v4: Intervalle de scan par symbole ---
        self._symbol_scan_intervals: Dict[str, int] = {}
        self._symbol_last_scan: Dict[str, float] = {}
        self._build_symbol_scan_intervals()

        # --- v4: Thread de sante ---
        self._health_thread = None
        self._health_running = False

        # --- v4: Compteur dry-run ---
        self._dry_run_trades = []
        self._dry_run_pnl = 0.0

    def _load_config(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _reload_config(self):
        """Recharge la configuration depuis le fichier."""
        try:
            new_config = self._load_config(self.config_path)
            new_config["_path"] = self.config_path
            new_config["_dry_run"] = self.dry_run
            self.config = deep_merge(self.config, new_config)
            self.scan_interval = self.config["timing"]["scan_interval_seconds"]
            self.auto_select = self.config.get("auto_market_select", {}).get("enabled", True)
            logger.info("Configuration rechargee avec succes.")
            return True
        except Exception as e:
            logger.error("Erreur rechargement config: %s", e)
            return False

    def _build_symbol_scan_intervals(self):
        """Definit des intervalles de scan specifiques par symbole."""
        base_interval = self.scan_interval
        volatile_multiplier = 0.5  # Les symboles volatils sont scannes 2x plus souvent
        for symbol in VOLATILE_SYMBOLS:
            self._symbol_scan_intervals[symbol] = max(5, int(base_interval * volatile_multiplier))

    def _get_symbol_scan_interval(self, symbol: str) -> int:
        """Retourne l'intervalle de scan pour un symbole donne."""
        return self._symbol_scan_intervals.get(symbol, self.scan_interval)

    def _should_scan_symbol(self, symbol: str) -> bool:
        """Verifie si un symbole doit etre scanne selon son intervalle."""
        now = time.time()
        interval = self._get_symbol_scan_interval(symbol)
        last_scan = self._symbol_last_scan.get(symbol, 0)
        return (now - last_scan) >= interval

    def _mark_symbol_scanned(self, symbol: str):
        """Enregistre le moment du dernier scan d'un symbole."""
        self._symbol_last_scan[symbol] = time.time()

    def _get_symbols_for_broker(self, broker: str) -> List[str]:
        """Retourne les symboles configures pour un broker donne."""
        brokers_cfg = self.config.get("brokers", {})
        broker_cfg = brokers_cfg.get(broker, {})
        symbols = broker_cfg.get("symbols", [])
        if symbols:
            return symbols
        # Fallback : symboles globaux filtres par type
        all_symbols = self.config.get("trading", {}).get("symbols", [])
        if broker == "mt5":
            mt5_only = [s for s in all_symbols if not s.startswith(("R_", "BOOM", "CRASH")) and not s.startswith("frx")]
            return mt5_only if mt5_only else all_symbols
        else:
            deriv_only = [s for s in all_symbols if s.startswith(("R_", "BOOM", "CRASH", "frx"))]
            return deriv_only if deriv_only else all_symbols

    def _init_connectors(self):
        """Initialise les connecteurs pour les brokers disponibles."""
        # Toujours essayer Deriv
        self.connectors["deriv"] = DerivConnector(self.config)
        logger.info("Connecteur Deriv initialise.")

        # Essayer MT5 si disponible
        if MT5_AVAILABLE and self.active_broker in ("mt5", "both"):
            self.connectors["mt5"] = MT5Connector(self.config)
            logger.info("Connecteur MT5 (Exness) initialise.")
        elif not MT5_AVAILABLE and self.active_broker in ("mt5",):
            logger.warning("MT5 demande mais MetaTrader5 pas installe. Deriv sera utilise.")
            self.active_broker = "deriv"

    def _init_trades_csv(self):
        if not self.config["logging"].get("log_trades_to_csv", True):
            return
        if not os.path.exists(self.trades_csv):
            with open(self.trades_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "broker", "symbol", "direction", "lot",
                    "entry_price", "sl", "tp", "exit_price",
                    "pnl", "reason", "score", "confidence", "market_score",
                    "mtf_confluence", "news_filtered"
                ])

    def _log_trade_csv(self, data: dict):
        if not self.config["logging"].get("log_trades_to_csv", True):
            return
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                data.get("broker", ""), data.get("symbol", ""), data.get("direction", ""),
                data.get("lot", ""), data.get("entry_price", ""),
                data.get("sl", ""), data.get("tp", ""),
                data.get("exit_price", ""), data.get("pnl", ""),
                data.get("reason", ""), data.get("score", ""),
                data.get("confidence", ""), data.get("market_score", ""),
                data.get("mtf_confluence", ""), data.get("news_filtered", ""),
            ])

    def _build_symbol_engines(self):
        profiles = self.config.get("symbol_profiles", {})
        all_symbols = set(self.config["trading"]["symbols"])
        # Ajouter les symboles specifiques par broker
        for bkr in ("deriv", "mt5"):
            for s in self._get_symbols_for_broker(bkr):
                all_symbols.add(s)
        for symbol in all_symbols:
            profile = profiles.get(symbol, {})
            if profile:
                sym_config = copy.deepcopy(self.config)
                if "indicators_override" in profile:
                    sym_config["indicators"] = deep_merge(sym_config["indicators"], profile["indicators_override"])
                if "strategy_weights" in profile:
                    sym_config["strategy_weights"] = profile["strategy_weights"]
                self._symbol_engines[symbol] = {
                    "ta": TechnicalAnalysis(sym_config),
                    "strategy": StrategyEngine(sym_config),
                }
            else:
                self._symbol_engines[symbol] = {"ta": self.technical, "strategy": self.strategy}

    def _get_symbol_profile(self, symbol: str) -> dict:
        return self.config.get("symbol_profiles", {}).get(symbol, {})

    def _get_active_brokers(self) -> List[str]:
        """Retourne la liste des brokers actifs."""
        if self.active_broker == "both":
            return [b for b in ["deriv", "mt5"] if b in self.connectors]
        elif self.active_broker in self.connectors:
            return [self.active_broker]
        else:
            return list(self.connectors.keys())

    # ==================================================================
    #  DIAGNOSTICS DE DEMARRAGE
    # ==================================================================

    def _run_startup_diagnostics(self) -> bool:
        """Verifie tous les composants au demarrage."""
        logger.info("=" * 60)
        logger.info("  DIAGNOSTICS DE DEMARRAGE")
        logger.info("=" * 60)
        all_ok = True

        # 1. Verifier le fichier de config
        logger.info("  [1/6] Fichier de config...")
        if os.path.exists(self.config_path):
            logger.info("        OK - Fichier present")
        else:
            logger.error("        ERREUR - Fichier introuvable!")
            all_ok = False

        # 2. Verifier les permissions d'ecriture
        logger.info("  [2/6] Permissions d'ecriture...")
        try:
            test_file = ".bot_write_test"
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            logger.info("        OK - Ecriture possible")
        except Exception as e:
            logger.error("        ERREUR - %s", e)
            all_ok = False

        # 3. Verifier la configuration
        logger.info("  [3/6] Validation configuration...")
        required_keys = ["trading", "timing", "logging", "brokers"]
        missing = [k for k in required_keys if k not in self.config]
        if missing:
            logger.error("        ERREUR - Cles manquantes: %s", missing)
            all_ok = False
        else:
            logger.info("        OK - Configuration valide")

        # 4. Verifier les symboles
        logger.info("  [4/6] Symboles configures...")
        for bkr in ("deriv", "mt5"):
            syms = self._get_symbols_for_broker(bkr)
            sym_display = ", ".join(syms[:3]) + ("..." if len(syms) > 3 else "")
            logger.info("        %s: %d symbole(s) - %s", bkr, len(syms), sym_display)

        # 5. Verifier MT5
        logger.info("  [5/6] MetaTrader 5...")
        if MT5_AVAILABLE:
            logger.info("        OK - MT5 disponible")
        else:
            logger.info("        INFO - MT5 non disponible (uniquement Deriv)")

        # 6. Verifier Telegram
        logger.info("  [6/6] Telegram...")
        tg = self.config.get("telegram", {})
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
        if tg.get("enabled") and telegram_token:
            logger.info("        OK - Telegram configure")
        else:
            logger.info("        INFO - Telegram non configure ou desactive")

        logger.info("=" * 60)
        if all_ok:
            logger.info("  Tous les diagnostics sont OK.")
        else:
            logger.warning("  Certains diagnostics ont echoue. Le bot peut fonctionner partiellement.")
        logger.info("=" * 60)
        return all_ok

    # ==================================================================
    #  SAUVEGARDE / CHARGEMENT D'ETAT
    # ==================================================================

    def _save_state(self):
        """Sauvegarde l'etat actuel du bot dans un fichier JSON."""
        try:
            state = {
                "active_broker": self.active_broker,
                "paused": self.paused,
                "cycle_count": self.cycle_count,
                "scan_interval": self.scan_interval,
                "timestamp": datetime.now().isoformat(),
                "daily_pnl": self.telegram._daily_pnl,
                "total_pnl": self.telegram._total_pnl,
                "trade_count": self.telegram._trade_count,
                "win_count": self.telegram._win_count,
                "loss_count": self.telegram._loss_count,
                "best_streak": self.telegram._best_streak,
                "worst_streak": self.telegram._worst_streak,
                "dry_run_trades": self._dry_run_trades[-20:],
                "dry_run_pnl": self._dry_run_pnl,
            }
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            logger.debug("Etat sauvegarde.")
        except Exception as e:
            logger.debug("Sauvegarde etat echouee: %s", e)

    def _load_state(self) -> dict:
        """Charge l'etat precedent si disponible."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    # ==================================================================
    #  HEALTH CHECK THREAD
    # ==================================================================

    def _start_health_check(self):
        """Demarre le thread de surveillance de la sante."""
        self._health_running = True
        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True, name="HealthCheck")
        self._health_thread.start()
        logger.info("Thread de sante demarre (verification toutes les 60s).")

    def _stop_health_check(self):
        """Arrete le thread de sante."""
        self._health_running = False
        if self._health_thread:
            self._health_thread.join(timeout=5)
        logger.info("Thread de sante arrete.")

    def _health_check_loop(self):
        """Boucle de verification de la sante de tous les composants."""
        while self._health_running and self.running:
            try:
                self._perform_health_check()
            except Exception as e:
                logger.debug("Health check erreur: %s", e)
            # Attendre 60 secondes
            for _ in range(60):
                if not self._health_running or not self.running:
                    break
                time.sleep(1)

    def _perform_health_check(self):
        """Verifie l'etat de tous les composants."""
        issues = []

        # Verifier chaque broker connecte
        for bkr_name, connector in self.connectors.items():
            broker_label = "Deriv" if bkr_name == "deriv" else "MT5"
            metrics = self._broker_metrics[bkr_name]

            if not self.dry_run and not connector.is_connected():
                issues.append("%s: deconnecte" % broker_label)
                continue

            # Verifier le taux de succes
            if metrics["total_requests"] > 10:
                success_rate = (metrics["success_count"] / metrics["total_requests"]) * 100
                avg_latency = metrics["total_latency_ms"] / metrics["success_count"] if metrics["success_count"] > 0 else 0

                # Notifier si la qualite est mauvaise
                self.telegram.notify_connection_quality(
                    broker_label, avg_latency, success_rate, metrics["error_count"]
                )

                if success_rate < 70:
                    issues.append("%s: taux de succes %.0f%%" % (broker_label, success_rate))
                if avg_latency > 3000:
                    issues.append("%s: latence %.0fms" % (broker_label, avg_latency))

            # Verifier le risk manager
            rm = self.risk_managers.get(bkr_name)
            if rm:
                report = rm.get_risk_report()
                risk_score = report.get("risk_score", 0)
                if risk_score >= 60:
                    self.telegram.notify_risk_alert(
                        risk_score, report.get("state", "inconnu"),
                        "DD: %+.2f$" % report.get("daily_pnl", 0), broker_label
                    )

        # Verifier l'arret d'urgence
        if self._check_emergency_stop():
            return

        # Auto-sauvegarde toutes les 5 minutes
        now = time.time()
        if now - self._last_auto_save >= self._auto_save_interval:
            self._save_state()
            self._last_auto_save = now

        if issues:
            logger.warning("Health check - Problemes detectes: %s", "; ".join(issues))
        else:
            logger.debug("Health check - Tous les composants sont en bonne sante.")

    def _check_emergency_stop(self) -> bool:
        """Verifie si l'arret d'urgence doit etre declenche."""
        if self._emergency_loss_threshold >= 0:
            return False  # Desactive si seuil positif

        total_pnl = self.telegram._daily_pnl
        if total_pnl <= self._emergency_loss_threshold:
            logger.critical(
                "ARRET D'URGENCE! Perte quotidienne (%+.2f$) depasse le seuil (%+.2f$)",
                total_pnl, self._emergency_loss_threshold
            )
            self.telegram.notify_error(
                "\U0001f6a8 <b>ARRET D'URGENCE</b>\n\n"
                "Perte quotidienne: <b>%+.2f$</b>\n"
                "Seuil d'arret: <b>%+.2f$</b>\n"
                "Le bot s'arrete automatiquement pour proteger le capital." % (total_pnl, self._emergency_loss_threshold)
            )
            self.paused = True
            self.telegram.notify_mode_change("pause", "Arret d'urgence - perte maximale atteinte")
            return True
        return False

    # ==================================================================
    #  METRIQUES DE PERFORMANCE
    # ==================================================================

    def _record_request_metric(self, broker: str, success: bool, latency_ms: float = 0):
        """Enregistre une metrique de requete pour un broker."""
        if broker not in self._broker_metrics:
            return
        metrics = self._broker_metrics[broker]
        metrics["total_requests"] += 1
        if success:
            metrics["success_count"] += 1
            metrics["total_latency_ms"] += latency_ms
            metrics["last_success_time"] = datetime.now().isoformat()
        else:
            metrics["error_count"] += 1
            metrics["last_error_time"] = datetime.now().isoformat()

    def _get_broker_metrics_summary(self, broker: str) -> str:
        """Retourne un resume des metriques pour un broker."""
        m = self._broker_metrics.get(broker, {})
        total = m.get("total_requests", 0)
        success = m.get("success_count", 0)
        errors = m.get("error_count", 0)
        if total == 0:
            return "Aucune requete"
        sr = (success / total) * 100
        avg_lat = m.get("total_latency_ms", 0) / success if success > 0 else 0
        return "Req: %d | OK: %d | Err: %d | SR: %.0f%% | Lat: %.0fms" % (total, success, errors, sr, avg_lat)

    # ==================================================================
    #  START / STOP
    # ==================================================================

    def start(self):
        setup_logging(self.config)
        logger = logging.getLogger(__name__)
        mode = "DRY-RUN" if self.dry_run else "LIVE TRADING"
        broker_label = {
            "deriv": "Deriv", "mt5": "Exness (MT5)", "both": "Deriv + Exness"
        }.get(self.active_broker, self.active_broker)

        # Diagnostics de demarrage
        self._run_startup_diagnostics()

        logger.info("=" * 60)
        logger.info("  ROBOT TRADING v4 - %s", mode)
        logger.info("  Brokers actifs : %s", broker_label)
        logger.info("  Selection auto marche : %s", "ON" if self.auto_select else "OFF")
        logger.info("  Multi-Timeframe : %s (%s)", "ON" if self.mtf.enabled else "OFF", self.mtf.mode)
        logger.info("  Filtre News : %s (%s)", "ON" if self.news_filter.enabled else "OFF", self.news_filter.mode)
        logger.info("  Telegram : %s", "ON" if self.telegram.enabled else "OFF")
        logger.info("  Commandes Telegram : %s", "ON" if self.cmd_bot.enabled else "OFF")
        logger.info("  Arret d'urgence : %+.2f$", self._emergency_loss_threshold)
        logger.info("=" * 60)

        # Demarrer Telegram avant les brokers pour garder le controle
        # du bot meme si une connexion broker echoue.
        self.cmd_bot.start()

        # Connexion des brokers avec degradation gracieuse. Le dry-run se
        # connecte aussi pour analyser les vrais cours, sans envoyer d'ordres.
        for bkr_name, connector in list(self.connectors.items()):
            broker_lbl = "Deriv" if bkr_name == "deriv" else "Exness (MT5)"
            is_active = bkr_name in self._get_active_brokers()
            if not is_active:
                continue
            logger.info("Connexion a %s (simulation=%s)...", broker_lbl, self.dry_run)
            try:
                start_t = time.time()
                if connector.connect():
                    latency = (time.time() - start_t) * 1000
                    self._record_request_metric(bkr_name, True, latency)
                    account = connector.get_account_info() or {
                        "balance": 5, "equity": 5, "currency": "USD"
                    }
                    self.risk_managers[bkr_name] = RiskManager(self.config, account)
                    self.compound_managers[bkr_name] = CompoundManager(self.config)
                    self.compound_managers[bkr_name].initialize(account.get("balance", 5))
                    logger.info("  %s OK | Donnees de marche disponibles", broker_lbl)
                else:
                    self._record_request_metric(bkr_name, False)
                    logger.error("  %s : connexion echouee.", broker_lbl)
                    del self.connectors[bkr_name]
            except Exception as e:
                self._record_request_metric(bkr_name, False)
                logger.error("  %s : erreur de connexion - %s", broker_lbl, e)
                del self.connectors[bkr_name]

        if not self.connectors:
            logger.error("Aucun broker connecte. Verifie les credentials et la connexion internet.")
            return

        # Ajuster le mode si un seul broker reste disponible.
        if self.active_broker == "both" and len(self.connectors) == 1:
            self.active_broker = list(self.connectors.keys())[0]
            logger.info("Mode ajuste a : %s (un seul broker disponible)", self.active_broker)

        self._initial_total_balance = sum(
            rm.balance for rm in self.risk_managers.values()
        )
        if self.dry_run:
            logger.info("Mode simulation - aucun ordre reel, analyse des vrais cours.")

        self.cmd_bot.notify_startup(self.active_broker)

        # Demarrer le thread de sante
        self._start_health_check()

        # News filter status
        if self.news_filter.enabled:
            safe, news_reason = self.news_filter.is_safe_to_trade()
            logger.info("News: %s", news_reason)
            if self.news_filter.upcoming_events:
                logger.info("%s", self.news_filter.status_report())

        # Multi-TF status
        if self.mtf.enabled:
            logger.info("%s", self.mtf.get_status_report())

        # Configurer SIGHUP pour le rechargement
        try:
            sig.signal(sig.SIGHUP, self._sighup_handler)
            logger.info("Signal SIGHUP configure pour le rechargement a chaud.")
        except (AttributeError, OSError):
            pass  # SIGHUP non disponible sur toutes les plateformes

        self.running = True
        logger.info("Robot demarre | Scan toutes les %ds", self.scan_interval)
        logger.info("Envoie /help sur Telegram pour les commandes.")
        sig.signal(sig.SIGINT, self._signal_handler)
        sig.signal(sig.SIGTERM, self._signal_handler)
        try:
            self._main_loop()
        finally:
            self.stop()

    def _signal_handler(self, signum, frame):
        logger.info("Arret recu...")
        self.running = False

    def _sighup_handler(self, signum, frame):
        """Gestionnaire du signal SIGHUP pour rechargement a chaud."""
        logger.info("SIGHUP recu - rechargement de la configuration...")
        if self._reload_config():
            self.telegram.notify_info("Configuration rechargee via SIGHUP.")
        else:
            self.telegram.notify_error("Echec du rechargement de la configuration via SIGHUP.")

    def stop(self):
        logger = logging.getLogger(__name__)
        # Arreter le health check
        self._stop_health_check()
        # Sauvegarder l'etat final
        self._save_state()
        # Notifier l'arret
        self.cmd_bot.notify_shutdown()
        # Arreter le command bot
        self.cmd_bot.stop()
        # Fermer toutes les positions sur tous les brokers
        if not self.dry_run:
            total_closed = 0
            for bkr_name, connector in self.connectors.items():
                try:
                    closed = connector.close_all_positions()
                    total_closed += closed
                    connector.disconnect()
                except Exception as e:
                    logger.error("Erreur fermeture %s: %s", bkr_name, e)
            logger.info("%d position(s) fermee(s) au total.", total_closed)
        # Stats croissance
        for bkr_name, cm in self.compound_managers.items():
            if cm.total_trades > 0:
                stats = cm.get_stats()
                logger.info("CROISSANCE %s: %s$ -> %s$ | Trades: %s | WinRate: %s%%",
                            bkr_name, stats["starting_balance"], stats["peak_balance"],
                            stats["total_trades"], stats["win_rate"])
        # Resume dry-run
        if self.dry_run and self._dry_run_trades:
            logger.info("DRY-RUN: %d trades simules, PnL: %+.2f$", len(self._dry_run_trades), self._dry_run_pnl)
        logger.info("Robot arrete.")

    # ==================================================================
    #  MAIN LOOP
    # ==================================================================

    def _main_loop(self):
        while self.running:
            cycle_start = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.error("Erreur cycle: %s", e, exc_info=True)
                self.telegram.notify_error("Erreur cycle: %s" % e)
            # Calculer le temps de sommeil restant
            cycle_elapsed = time.time() - cycle_start
            sleep_time = max(1, self.scan_interval - cycle_elapsed)
            # Decouper le sommeil pour reagir rapidement a l'arret
            for _ in range(int(sleep_time)):
                if not self.running:
                    break
                time.sleep(1)

    def _scan_cycle(self):
        logger = logging.getLogger(__name__)
        self.cycle_count += 1

        if self.paused:
            if self.cycle_count % 20 == 0:
                logger.debug("En pause - pas de scan.")
            return

        # Scanner chaque broker actif (degradation gracieuse si erreur)
        for bkr_name in self._get_active_brokers():
            if not self.running:
                break
            try:
                self._scan_cycle_for_broker(bkr_name)
            except Exception as e:
                logger.error("Erreur cycle %s: %s", bkr_name, e, exc_info=True)
                self._record_request_metric(bkr_name, False)
                # Degradation gracieuse: continuer avec le prochain broker
                logger.info("Degradation gracieuse: erreur %s ignoree, continuation.", bkr_name)

        # Rapport Telegram quotidien (toutes les 60 cycles ~ 30 min)
        if self.telegram.enabled and self.cycle_count % 60 == 0:
            self._send_daily_report()

        # Rapport hebdomadaire
        if self.telegram.enabled and self.cycle_count % 60 == 0:
            self._send_weekly_report()

    def _scan_cycle_for_broker(self, bkr_name: str):
        logger = logging.getLogger(__name__)
        connector = self.connectors.get(bkr_name)
        if not connector:
            return
        rm = self.risk_managers.get(bkr_name)
        if not rm:
            return

        broker_label = "Deriv" if bkr_name == "deriv" else "MT5"

        if not self.dry_run:
            if not connector.is_connected():
                logger.warning("[%s] Reconnexion...", broker_label)
                start_t = time.time()
                if not connector.connect():
                    self._record_request_metric(bkr_name, False, (time.time() - start_t) * 1000)
                    self.telegram.notify_error("Reconnexion %s echouee!" % broker_label)
                    return
                self._record_request_metric(bkr_name, True, (time.time() - start_t) * 1000)
                self.telegram.notify_connection_status(broker_label, True)
            start_t = time.time()
            account = connector.get_account_info()
            lat = (time.time() - start_t) * 1000
            self._record_request_metric(bkr_name, True, lat)
            if account:
                rm.update_account(account)
                cm = self.compound_managers.get(bkr_name)
                if cm:
                    cm.update_peak(account.get("balance", 0))

        self._manage_open_positions(bkr_name)
        self._check_closed_trades(bkr_name)

        if self.auto_select:
            self._smart_scan(bkr_name)
        else:
            self._scan_for_signals(bkr_name)

        if not self.dry_run and rm:
            report = rm.get_risk_report()
            positions = connector.get_bot_positions()
            total_pnl = sum(p.get("profit", 0) for p in positions)
            balance = report["equity"]
            cm = self.compound_managers.get(bkr_name)
            progress = cm.get_progress_message(balance) if cm else ""
            metrics = self._get_broker_metrics_summary(bkr_name)
            logger.info("[%s] Solde: %.2f$ | PnL: %+.2f$ | %s | %s",
                        broker_label, balance, total_pnl, progress, metrics)

    def _send_daily_report(self):
        if self.telegram.last_report_date == datetime.now().strftime("%d/%m/%Y"):
            return
        # Agreger les stats de tous les brokers
        total_balance = 0
        total_daily_pnl = 0
        total_trades = 0
        total_wins = 0
        total_losses = 0
        peak = 0
        for bkr_name, cm in self.compound_managers.items():
            stats = cm.get_stats()
            total_trades += stats["total_trades"]
            total_balance += stats.get("current_balance", 0)
            peak += stats["peak_balance"]
            rm = self.risk_managers.get(bkr_name)
            if rm:
                report = rm.get_risk_report()
                total_daily_pnl += report.get("daily_pnl", 0)
        total_wins = self.telegram._win_count
        total_losses = self.telegram._loss_count
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        dd = 0
        for cm in self.compound_managers.values():
            if hasattr(cm, "get_max_drawdown"):
                dd = max(dd, cm.get_max_drawdown())
        self.telegram.notify_daily_report(
            balance=total_balance,
            daily_pnl=total_daily_pnl,
            total_trades=total_trades,
            win_rate=win_rate,
            peak_balance=peak,
            compound_msg="Multi-broker",
            max_drawdown=dd,
        )

    def _send_weekly_report(self):
        """Envoie le rapport hebdomadaire si applicable."""
        if datetime.now().weekday() != 0:  # Lundi seulement
            return
        total_balance = 0
        peak = 0
        dd = 0
        for bkr_name, cm in self.compound_managers.items():
            stats = cm.get_stats()
            total_balance += stats.get("current_balance", 0)
            peak += stats["peak_balance"]
            if hasattr(cm, "get_max_drawdown"):
                dd = max(dd, cm.get_max_drawdown())
        wr = (self.telegram._weekly_wins / self.telegram._weekly_trades * 100) if self.telegram._weekly_trades > 0 else 0
        self.telegram.notify_weekly_report(
            balance=total_balance,
            weekly_pnl=self.telegram._weekly_pnl,
            total_trades=self.telegram._weekly_trades,
            win_rate=wr,
            peak_balance=peak,
            max_drawdown=dd,
        )

    # ==================================================================
    #  TRADING LOGIC (par broker)
    # ==================================================================

    def _smart_scan(self, bkr_name: str):
        """Scan intelligent avec multi-TF + filtre news + scan specifique par symbole."""
        logger = logging.getLogger(__name__)
        connector = self.connectors[bkr_name]
        rm = self.risk_managers.get(bkr_name)
        if not rm:
            return
        broker_label = "Deriv" if bkr_name == "deriv" else "MT5"
        symbols = self._get_symbols_for_broker(bkr_name)
        current_pos_count = connector.get_position_count() if not self.dry_run else 0

        can_trade, reason = rm.can_open_position(current_pos_count)
        if not can_trade:
            logger.debug("[%s] Pas de trade : %s", broker_label, reason)
            return

        # Filtre news global
        if not self.dry_run:
            safe, news_reason = self.news_filter.is_safe_to_trade()
            if not safe:
                logger.info("[%s] News Filter: %s", broker_label, news_reason)
                self.telegram.notify_news_filter("[%s] %s" % (broker_label, news_reason))
                return

        evaluations = {}
        for symbol in symbols:
            if not self.running:
                break
            # Verifier l'intervalle de scan specifique au symbole
            if not self._should_scan_symbol(symbol):
                continue
            try:
                ev = self._evaluate_market(bkr_name, symbol)
                if ev is not None:
                    evaluations[symbol] = ev
                self._mark_symbol_scanned(symbol)
            except Exception as e:
                logger.debug("[%s] Eval %s echec: %s", broker_label, symbol, e)
                self._record_request_metric(bkr_name, False)

        if not evaluations:
            return

        ranked = sorted(evaluations.items(), key=lambda x: x[1]["score"], reverse=True)
        logger.info("--- [%s] CLASSEMENT ---", broker_label)
        for sym, ev in ranked:
            bd = ev["breakdown"]
            trend = bd.get("trend_dir", "?")
            rsi = bd.get("rsi", "?")
            mtf = ev.get("mtf", {}).get("confluence_score", "?")
            logger.info("  %-12s | Score=%5.1f/100 | %-10s | T=%s | RSI=%s | MTF=%s",
                        sym, ev["score"], ev["recommendation"], trend, rsi, mtf)

        best = self.market_selector.select_best_market(evaluations)
        if best is None:
            return

        self._execute_trade_on_best(bkr_name, best, evaluations[best])

    def _evaluate_market(self, bkr_name: str, symbol: str) -> Optional[Dict]:
        connector = self.connectors[bkr_name]
        start_t = time.time()
        ohlc = connector.get_ohlc_data(symbol)
        lat = (time.time() - start_t) * 1000
        if ohlc is None:
            self._record_request_metric(bkr_name, False, lat)
            return None
        self._record_request_metric(bkr_name, True, lat)
        engine = self._symbol_engines.get(symbol)
        ta = engine["ta"] if engine else self.technical
        strategy = engine["strategy"] if engine else self.strategy
        analysis = ta.full_analysis(ohlc)
        latest = ta.get_latest_values(analysis)
        prices = connector.get_current_price(symbol)
        if prices is None:
            return None
        current_price = prices[1]
        evaluation = self.market_selector.evaluate_market(symbol, analysis, latest, current_price)
        signal_result = strategy.generate_signal(latest, current_price)
        if signal_result["signal"] != "HOLD" and self.mtf.enabled:
            mtf_result = self.mtf.analyze(connector, symbol, analysis, latest, signal_result["signal"])
            evaluation["mtf"] = mtf_result
            if not mtf_result["confirmed"]:
                if self.mtf.mode == "strict":
                    evaluation["score"] *= 0.3
        return evaluation

    def _execute_trade_on_best(self, bkr_name: str, symbol: str, market_eval: Dict):
        logger = logging.getLogger(__name__)
        connector = self.connectors[bkr_name]
        rm = self.risk_managers.get(bkr_name)
        broker_label = "Deriv" if bkr_name == "deriv" else "MT5"

        if not self._is_trading_session():
            return

        # Mode dry-run ameliore: simuler le trade
        if self.dry_run:
            mtf_info = " | MTF=%s" % market_eval.get("mtf", {}).get("confluence_score", "?")
            logger.info("[DRY-RUN][%s] Meilleur marche : %s (score=%s%s)",
                        broker_label, symbol, market_eval["score"], mtf_info)
            # Simuler un resultat
            import random
            simulated_pnl = random.uniform(-2, 3) * (market_eval["score"] / 100)
            self._dry_run_pnl += simulated_pnl
            self._dry_run_trades.append({
                "symbol": symbol, "score": market_eval["score"],
                "pnl": simulated_pnl, "time": datetime.now().isoformat()
            })
            if len(self._dry_run_trades) > 100:
                self._dry_run_trades = self._dry_run_trades[-100:]
            return

        if connector.get_positions_by_symbol(symbol):
            return

        ohlc = connector.get_ohlc_data(symbol)
        if ohlc is None:
            return
        engine = self._symbol_engines.get(symbol)
        ta = engine["ta"] if engine else self.technical
        strategy = engine["strategy"] if engine else self.strategy
        analysis = ta.full_analysis(ohlc)
        latest = ta.get_latest_values(analysis)
        prices = connector.get_current_price(symbol)
        if prices is None:
            return
        current_price = prices[1]
        signal_result = strategy.generate_signal(latest, current_price)
        signal = signal_result["signal"]
        confidence = signal_result["confidence"]

        if signal == "HOLD":
            return

        # Multi-TF confirmation
        mtf_confluence = 0
        mtf_lot_mult = 1.0
        if self.mtf.enabled:
            mtf_result = self.mtf.analyze(connector, symbol, analysis, latest, signal)
            mtf_confluence = mtf_result["confluence_score"]
            mtf_lot_mult = mtf_result["lot_multiplier"]
            if not mtf_result["confirmed"]:
                logger.info("[%s] %s: signal non confirme multi-TF.", broker_label, symbol)
                self.telegram.notify_mtf_block("[%s] %s" % (broker_label, symbol), mtf_result["details"])
                return
            signal = mtf_result["signal"]

        # News filter
        safe, news_reason = self.news_filter.is_safe_to_trade(symbol)
        if not safe:
            logger.info("[%s] %s: trade bloque par news. (%s)", broker_label, symbol, news_reason)
            return
        news_lot_mult = self.news_filter.get_lot_multiplier(symbol)

        # Calculer la mise
        balance = rm.balance
        cm = self.compound_managers.get(bkr_name)
        if cm:
            lot = cm.calculate_stake(balance, confidence)
        else:
            lot = self.config["trading"]["default_lot_size"]
        lot = lot * mtf_lot_mult * news_lot_mult
        lot_step = self.config["trading"].get("lot_step", 0.35)
        lot = max(lot_step, round(lot / lot_step) * lot_step)
        profile = self._get_symbol_profile(symbol)
        max_lot = profile.get("max_lot", self.config["trading"]["max_lot_size"])
        lot = min(lot, max_lot)

        logger.info("** [%s] TRADE %s | %s | Score=%s | MTF=%s | News x%s | Confiance=%s%% | Mise=%s$ **",
                    broker_label, symbol, signal, market_eval["score"],
                    mtf_confluence, news_lot_mult, confidence, lot)

        order_types = ORDER_TYPES.get(bkr_name, ORDER_TYPES["deriv"])
        order_type = order_types["buy"] if signal == "BUY" else order_types["sell"]
        comment = "BOT_%s_%s%%_M%s_TF%s" % (signal, confidence, market_eval["score"], mtf_confluence)

        start_t = time.time()
        result = connector.open_position(
            symbol=symbol, order_type=order_type,
            lot=lot, sl=0, tp=0, comment=comment,
        )
        lat = (time.time() - start_t) * 1000

        if result and result.get("success"):
            self._record_request_metric(bkr_name, True, lat)
            self._log_trade_csv({
                "broker": bkr_name, "symbol": symbol, "direction": signal, "lot": lot,
                "entry_price": result["price"], "sl": 0, "tp": 0,
                "score": signal_result["total_score"],
                "confidence": confidence, "market_score": market_eval["score"],
                "mtf_confluence": mtf_confluence, "news_filtered": "no",
            })
            self.telegram.notify_trade_open(
                "[%s] %s" % (broker_label, symbol), signal, lot, confidence, market_eval["score"], mtf_confluence
            )
            if "deal" in result:
                self._trade_open_times[result["deal"]] = datetime.now()
        else:
            self._record_request_metric(bkr_name, False, lat)

    def _manage_open_positions(self, bkr_name: str):
        if self.dry_run:
            return
        connector = self.connectors[bkr_name]
        rm = self.risk_managers.get(bkr_name)
        if not rm:
            return
        positions = connector.get_bot_positions()
        for pos in positions:
            symbol = pos["symbol"]
            prices = connector.get_current_price(symbol)
            if prices is None:
                continue
            current_price = prices[0] if pos["type"] == 0 else prices[1]
            pip_size = connector.get_pip_size(symbol)
            profile = self._get_symbol_profile(symbol)
            orig = {
                "trail": rm.trailing_stop_pips,
                "be": rm.break_even_after_pips,
                "move": rm.move_sl_to_be_pips,
            }
            if "trailing_stop_pips" in profile:
                rm.trailing_stop_pips = profile["trailing_stop_pips"]
            if "break_even_after_pips" in profile:
                rm.break_even_after_pips = profile["break_even_after_pips"]
            if "move_sl_to_be_pips" in profile:
                rm.move_sl_to_be_pips = profile["move_sl_to_be_pips"]
            new_sl = rm.check_trailing_stop(pos, current_price, pip_size)
            if new_sl is not None:
                connector.modify_position_sl(pos["ticket"], new_sl)
            else:
                new_sl = rm.check_breakeven(pos, current_price, pip_size)
                if new_sl is not None:
                    connector.modify_position_sl(pos["ticket"], new_sl)
            rm.trailing_stop_pips = orig["trail"]
            rm.break_even_after_pips = orig["be"]
            rm.move_sl_to_be_pips = orig["move"]

    def _check_closed_trades(self, bkr_name: str):
        if self.dry_run:
            return
        connector = self.connectors[bkr_name]
        rm = self.risk_managers.get(bkr_name)
        cm = self.compound_managers.get(bkr_name)
        broker_label = "Deriv" if bkr_name == "deriv" else "MT5"
        deals = connector.get_recent_deals(days=1)
        snap_key = "%s_deals" % bkr_name
        if snap_key not in self._positions_snapshots:
            self._positions_snapshots[snap_key] = {}
        snapshot = self._positions_snapshots[snap_key]
        for deal in deals:
            ticket = deal["ticket"]
            if ticket not in snapshot:
                snapshot[ticket] = True
                pnl = deal["profit"]
                if rm:
                    rm.record_trade(pnl)
                if cm:
                    cm.record_trade_result(pnl)
                self._log_trade_csv({
                    "broker": bkr_name,
                    "symbol": deal["symbol"],
                    "direction": "BUY" if deal["type"] == 0 else "SELL",
                    "exit_price": deal["price"], "pnl": pnl, "reason": "closed",
                })
                duration = 0
                open_time = self._trade_open_times.pop(ticket, None)
                if open_time:
                    duration = (datetime.now() - open_time).total_seconds() / 60
                self.telegram.notify_trade_close(
                    "[%s] %s" % (broker_label, deal["symbol"]), pnl, "closed", duration
                )

    def _scan_for_signals(self, bkr_name: str):
        symbols = self._get_symbols_for_broker(bkr_name)
        connector = self.connectors[bkr_name]
        rm = self.risk_managers.get(bkr_name)
        current_pos_count = connector.get_position_count() if not self.dry_run else 0
        for symbol in symbols:
            if not self.running:
                break
            # Verifier l'intervalle de scan specifique au symbole
            if not self._should_scan_symbol(symbol):
                continue
            try:
                self._analyze_symbol(bkr_name, symbol, current_pos_count)
                self._mark_symbol_scanned(symbol)
            except Exception as e:
                logger.error("Erreur %s/%s: %s", bkr_name, symbol, e, exc_info=True)
                self._record_request_metric(bkr_name, False)

    def _analyze_symbol(self, bkr_name: str, symbol: str, current_pos_count: int):
        logger = logging.getLogger(__name__)
        connector = self.connectors[bkr_name]
        rm = self.risk_managers.get(bkr_name)
        if not rm:
            return
        if not self._is_trading_session():
            return

        if not self.dry_run and connector.get_positions_by_symbol(symbol):
            return
        can_trade, reason = rm.can_open_position(current_pos_count)
        if not can_trade:
            return
        safe, news_reason = self.news_filter.is_safe_to_trade(symbol)
        if not safe:
            logger.info("News Filter %s: %s", symbol, news_reason)
            return
        ohlc = connector.get_ohlc_data(symbol)
        if ohlc is None:
            return
        engine = self._symbol_engines.get(symbol)
        ta = engine["ta"] if engine else self.technical
        strategy = engine["strategy"] if engine else self.strategy
        analysis = ta.full_analysis(ohlc)
        latest = ta.get_latest_values(analysis)
        prices = connector.get_current_price(symbol)
        if prices is None:
            return
        current_price = prices[1]
        signal_result = strategy.generate_signal(latest, current_price)
        signal = signal_result["signal"]
        if signal == "HOLD":
            return
        mtf_confluence = 0
        mtf_lot_mult = 1.0
        if self.mtf.enabled:
            mtf_result = self.mtf.analyze(connector, symbol, analysis, latest, signal)
            mtf_confluence = mtf_result["confluence_score"]
            mtf_lot_mult = mtf_result["lot_multiplier"]
            if not mtf_result["confirmed"]:
                return
            signal = mtf_result["signal"]
        news_lot_mult = self.news_filter.get_lot_multiplier(symbol)
        balance = rm.balance
        cm = self.compound_managers.get(bkr_name)
        if cm:
            lot = cm.calculate_stake(balance, signal_result["confidence"])
        else:
            lot = self.config["trading"]["default_lot_size"]
        lot = lot * mtf_lot_mult * news_lot_mult
        order_types = ORDER_TYPES.get(bkr_name, ORDER_TYPES["deriv"])
        order_type = order_types["buy"] if signal == "BUY" else order_types["sell"]

        if self.dry_run:
            import random
            simulated_pnl = random.uniform(-1.5, 2.5)
            self._dry_run_pnl += simulated_pnl
            self._dry_run_trades.append({
                "broker": bkr_name, "symbol": symbol, "direction": signal,
                "confidence": signal_result["confidence"], "pnl": simulated_pnl,
                "time": datetime.now().isoformat()
            })
            if len(self._dry_run_trades) > 100:
                self._dry_run_trades = self._dry_run_trades[-100:]
            logger.info(
                "[DRY-RUN][%s] %s %s | confiance=%s | lot=%.2f | PnL simule=%+.2f",
                bkr_name.upper(), signal, symbol, signal_result["confidence"], lot, simulated_pnl
            )
            self.telegram.notify_trade_open(
                "[DRY-RUN][%s] %s" % (bkr_name.upper(), symbol),
                signal, lot, signal_result["confidence"]
            )
            self.telegram.notify_info(
                "[DRY-RUN] Aucun ordre reel envoye. Signal %s %s | PnL simule: %+.2f$"
                % (signal, symbol, simulated_pnl)
            )
            return

        result = connector.open_position(
            symbol=symbol, order_type=order_type, lot=lot, sl=0, tp=0,
            comment="BOT_%s_TF%.1f" % (signal, mtf_confluence)
        )
        if result and result.get("success"):
            self._log_trade_csv({
                "broker": bkr_name, "symbol": symbol, "direction": signal, "lot": lot,
                "entry_price": result["price"], "sl": 0, "tp": 0,
                "score": signal_result["total_score"],
                "confidence": signal_result["confidence"],
                "reason": "signal", "mtf_confluence": mtf_confluence
            })
            self.telegram.notify_trade_open(
                "[%s] %s" % (bkr_name.upper(), symbol), signal, lot, signal_result["confidence"]
            )
            if "deal" in result:
                self._trade_open_times[result["deal"]] = datetime.now()

    def _is_trading_session(self) -> bool:
        sc = self.config.get("session_filter", {})
        if not sc.get("enabled", True):
            return True
        now = datetime.now()
        if sc.get("avoid_weekend", True) and now.weekday() >= 5:
            return False
        if now.weekday() == 4 and now.hour >= sc.get("friday_close_hour", 20):
            return False
        return True

    # ==================================================================
    #  TELEGRAM COMMAND CALLBACKS (ETENDUS v4)
    # ==================================================================

    def _telegram_switch_broker(self, target: str) -> str:
        """Switch de broker appele depuis Telegram."""
        logger = logging.getLogger(__name__)
        logger.info("Switch broker demande : %s -> %s", self.active_broker, target)

        if target == self.active_broker:
            label = {"deriv": "Deriv", "mt5": "Exness", "both": "Les deux"}.get(target, target)
            return "\u2139\ufe0f Deja en mode <b>%s</b>." % label

        # Si switch vers mt5 mais pas disponible
        if target in ("mt5", "both") and "mt5" not in self.connectors and not MT5_AVAILABLE:
            return (
                "\u26a0\ufe0f <b>MT5 non disponible</b>\n"
                "MetaTrader 5 n'est pas installe sur cette machine.\n"
                "Seul Deriv est disponible."
            )

        # Sauvegarder l'etat avant le switch
        self._save_state()

        # Si on demande mt5 seul mais pas encore connecte
        if target == "mt5" and "mt5" not in self.connectors and MT5_AVAILABLE:
            mt5_conn = MT5Connector(self.config)
            if mt5_conn.connect():
                account = mt5_conn.get_account_info()
                self.connectors["mt5"] = mt5_conn
                self.risk_managers["mt5"] = RiskManager(self.config, account)
                self.compound_managers["mt5"] = CompoundManager(self.config)
                self.compound_managers["mt5"].initialize(account.get("balance", 5))
                self.telegram.notify_connection_status("Exness (MT5)", True)
            else:
                return "\u274c <b>Connexion MT5 echouee.</b>\nImpossible de switcher vers Exness."

        # Si on demande both et qu'un connecteur manque
        if target == "both":
            for bkr in ("deriv", "mt5"):
                if bkr not in self.connectors:
                    if bkr == "mt5" and MT5_AVAILABLE:
                        mt5_conn = MT5Connector(self.config)
                        if mt5_conn.connect():
                            account = mt5_conn.get_account_info()
                            self.connectors["mt5"] = mt5_conn
                            self.risk_managers["mt5"] = RiskManager(self.config, account)
                            self.compound_managers["mt5"] = CompoundManager(self.config)
                            self.compound_managers["mt5"].initialize(account.get("balance", 5))
                            self.telegram.notify_connection_status("Exness (MT5)", True)

        old = self.active_broker
        self.active_broker = target
        label = {"deriv": "Deriv", "mt5": "Exness", "both": "Deriv + Exness"}.get(target, target)
        old_label = {"deriv": "Deriv", "mt5": "Exness", "both": "Les deux"}.get(old, old)

        # Mettre a jour la config
        self.config["active_broker"] = target

        # Notifier le changement de mode
        self.telegram.notify_mode_change("switch_broker", "%s -> %s" % (old_label, label))

        return (
            "\u2705 <b>SWITCH EFFECTUE</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\u2b05\ufe0f Avant : %s\n"
            "\u27a1\ufe0f Maintenant : <b>%s</b>\n"
            "\U0001f4c8 Etat sauvegarde avant le switch\n"
            "\U0001f552 %s" % (old_label, label, datetime.now().strftime("%H:%M:%S"))
        )

    def _telegram_stop(self):
        logger = logging.getLogger(__name__)
        logger.info("Arret demande via Telegram.")
        self._save_state()
        self.running = False

    def _telegram_pause(self):
        logger = logging.getLogger(__name__)
        self.paused = True
        logger.info("Pause demande via Telegram.")
        self.telegram.notify_mode_change("pause")

    def _telegram_resume(self):
        logger = logging.getLogger(__name__)
        self.paused = False
        logger.info("Reprise demande via Telegram.")
        self.telegram.notify_mode_change("resume")

    def _telegram_status(self) -> str:
        """Retourne le statut complet du robot."""
        label = {"deriv": "Deriv", "mt5": "Exness", "both": "Deriv + Exness"}.get(self.active_broker, self.active_broker)
        mode = "PAUSE" if self.paused else "ACTIF"
        mode_emoji = "\u23f8\ufe0f" if self.paused else "\u25b6\ufe0f"
        uptime = self.cmd_bot.get_session_duration()
        mode_tag = "DRY-RUN" if self.dry_run else "LIVE"

        text = (
            "\U0001f4ca <b>STATUT ROBOT v4</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "%s Mode : <b>%s</b> (%s)\n"
            "\U0001f310 Broker : <b>%s</b>\n"
            "\U0001f552 Uptime : %s\n"
            "\U0001f504 Cycles : %d\n"
            "\U0001f504 Scan : %ds\n\n"
        ) % (mode_emoji, mode, mode_tag, label, uptime, self.cycle_count, self.scan_interval)

        for bkr_name, connector in self.connectors.items():
            broker_label = "Deriv" if bkr_name == "deriv" else "Exness"
            connected = "\U0001f7e2" if connector.is_connected() else "\U0001f534"
            account = connector.get_account_info()
            balance = account.get("balance", 0) if account else 0
            positions = connector.get_bot_positions() if connector.is_connected() else []
            pnl = sum(p.get("profit", 0) for p in positions)
            is_active = bkr_name in self._get_active_brokers()
            active_tag = " \u2b50 ACTIF" if is_active else " (standby)"
            metrics = self._get_broker_metrics_summary(bkr_name)
            text += (
                "%s <b>%s</b>%s\n"
                "  Solde: %.2f$ | PnL: %+.2f$ | Pos: %d\n"
                "  %s\n"
            ) % (connected, broker_label, active_tag, balance, pnl, len(positions), metrics)

        tc = self.telegram._trade_count
        wc = self.telegram._win_count
        lc = self.telegram._loss_count
        wr = (wc / tc * 100) if tc > 0 else 0
        text += (
            "\n\U0001f4c8 Session: %d trades | %dW/%dL | WR: %.0f%%\n"
            "\U0001f4b8 PnL jour: %+.2f$ | Total: %+.2f$"
        ) % (tc, wc, lc, wr, self.telegram._daily_pnl, self.telegram._total_pnl)

        if self.dry_run:
            text += (
                "\n\n\U0001f4dd <b>DRY-RUN</b>\n"
                "  Trades simules: %d\n"
                "  PnL simule: %+.2f$"
            ) % (len(self._dry_run_trades), self._dry_run_pnl)
        return text

    def _telegram_balance(self) -> str:
        """Retourne le solde detaille par broker."""
        text = "\U0001f4b0 <b>SOLDES DETAILLES</b>\n"
        text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        for bkr_name, connector in self.connectors.items():
            broker_label = "Deriv" if bkr_name == "deriv" else "Exness (MT5)"
            account = connector.get_account_info()
            if account:
                balance = account.get("balance", 0)
                equity = account.get("equity", balance)
                currency = account.get("currency", "USD")
                cm = self.compound_managers.get(bkr_name)
                level = ""
                progress = ""
                if cm:
                    lvl = cm.get_current_level(balance)
                    level = "\n  Niveau: %s" % lvl["current"]["label"]
                    progress = "\n  Progression: %s" % cm.get_progress_message(balance)
                rm = self.risk_managers.get(bkr_name)
                risk_info = ""
                if rm:
                    report = rm.get_risk_report()
                    risk_info = "\n  PnL jour: %+.2f$ | Score: %.0f/100" % (report.get("daily_pnl", 0), report.get("risk_score", 0))
                text += (
                    "\U0001f310 <b>%s</b>\n"
                    "  Solde: <b>%.2f %s</b>\n"
                    "  Equity: %.2f %s"
                    "%s%s%s\n\n"
                ) % (broker_label, balance, currency, equity, currency, level, progress, risk_info)
            else:
                text += "\U0001f310 <b>%s</b> : Non connecte\n\n" % broker_label
        return text

    def _telegram_positions(self) -> str:
        """Retourne les positions ouvertes avec plus de details."""
        text = "\U0001f4c8 <b>POSITIONS OUVERTES</b>\n"
        text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        has_positions = False
        for bkr_name, connector in self.connectors.items():
            broker_label = "Deriv" if bkr_name == "deriv" else "MT5"
            positions = connector.get_bot_positions()
            for pos in positions:
                has_positions = True
                symbol = pos["symbol"]
                direction = "BUY" if pos["type"] == 0 else "SELL"
                emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"
                pnl = pos.get("profit", 0)
                pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
                volume = pos.get("volume", 0)
                entry = pos.get("price_open", 0)
                ticket = pos.get("ticket", "?")
                text += (
                    "%s <b>[%s] %s</b> %s\n"
                    "  Ticket: %s\n"
                    "  Mise: %.2f$ | Entree: %s\n"
                    "  PnL: %s <b>%+.2f$</b>\n\n"
                ) % (emoji, broker_label, symbol, direction, ticket, volume, entry, pnl_emoji, pnl)
        if not has_positions:
            text += "Aucune position ouverte."
        return text

    def _telegram_trades(self, n: int = 10) -> str:
        """Retourne les derniers N trades."""
        import csv as csv_mod
        trades_file = self.trades_csv
        if not os.path.exists(trades_file):
            return (
                "\U0001f4c4 <b>DERNIERS %d TRADES</b>\n\n"
                "Aucun fichier de trades trouve."
            ) % n
        try:
            with open(trades_file, "r", encoding="utf-8") as f:
                reader = list(csv_mod.reader(f))
            if len(reader) <= 1:
                return "\U0001f4c4 <b>DERNIERS %d TRADES</b>\n\nAucun trade enregistre." % n
            headers = reader[0]
            rows = reader[1:]
            last_rows = rows[-n:]
            total = len(rows)

            text = "\U0001f4c4 <b>DERNIERS %d TRADES</b> (sur %d)\n" % (len(last_rows), total)
            text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"

            for row in reversed(last_rows):
                rd = dict(zip(headers, row)) if len(row) == len(headers) else {}
                ts = rd.get("timestamp", row[0] if row else "?")
                sym = rd.get("symbol", row[2] if len(row) > 2 else "?")
                d = rd.get("direction", row[3] if len(row) > 3 else "?")
                pnl_s = rd.get("pnl", row[9] if len(row) > 9 else "")
                bkr = rd.get("broker", row[1] if len(row) > 1 else "?")
                try:
                    pv = float(pnl_s) if pnl_s else 0
                except (ValueError, TypeError):
                    pv = 0
                if pv > 0:
                    pe = "\u2705"
                elif pv < 0:
                    pe = "\u274c"
                else:
                    pe = "\u23f8\ufe0f"
                de = "\U0001f7e2" if d.upper() == "BUY" else "\U0001f534"
                bt = "[%s] " % bkr.upper() if bkr else ""
                text += (
                    "%s %s%s <b>%s</b> %s\n"
                    "  PnL: <b>%+.2f$</b> | %s\n"
                ) % (pe, bt, de, sym, d, pv, ts)
            return text
        except Exception as e:
            return "\U0001f4c4 <b>ERREUR</b>\n\n%s" % str(e)

    def _telegram_risk(self) -> str:
        """Retourne le score de risque et l'etat du risk manager."""
        text = "\u26a0\ufe0f <b>ANALYSE DE RISQUE</b>\n"
        text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"

        has_risk = False
        for bkr_name, rm in self.risk_managers.items():
            broker_label = "Deriv" if bkr_name == "deriv" else "Exness"
            report = rm.get_risk_report()
            has_risk = True

            score = report.get("risk_score", 0)
            state = report.get("state", "inconnu")
            daily_pnl = report.get("daily_pnl", 0)
            equity = report.get("equity", 0)
            dd = report.get("drawdown_pct", 0)

            if score >= 70:
                score_emoji = "\U0001f534"
            elif score >= 40:
                score_emoji = "\U0001f7e1"
            else:
                score_emoji = "\U0001f7e2"

            risk_block = (
                "\U0001f310 <b>%s</b>\n"
                "  %s Score risque: <b>%.0f/100</b> (%s)\n"
                "  \U0001f4b8 Equity: %.2f$\n"
                "  \U0001f4c9 PnL jour: %+.2f$\n"
                "  \U0001f4c9 Drawdown: %.1f%%\n"
            ) % (broker_label, score_emoji, score, state, equity, daily_pnl, dd)

            # Limites de risque
            if hasattr(rm, "max_daily_loss"):
                risk_block += "  \U0001f6d1 Max perte/jour: %.2f$\n" % rm.max_daily_loss
            if hasattr(rm, "max_positions"):
                risk_block += "  \U0001f4cb Max positions: %d\n" % rm.max_positions
            risk_block += "\n"
            text += risk_block

        # Seuil d'urgence
        text += "\U0001f6a8 Seuil arret urgence: <b>%+.2f$/jour</b>\n" % self._emergency_loss_threshold
        text += "\U0001f4c9 Perte actuelle: <b>%+.2f$</b>\n" % self.telegram._daily_pnl

        if not has_risk:
            text += "Aucun risk manager initialise."
        return text

    def _telegram_compound(self) -> str:
        """Retourne le niveau de croissance composee et la progression."""
        text = "\U0001f3c6 <b>CROISSANCE COMPOSEE</b>\n"
        text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"

        has_compound = False
        for bkr_name, cm in self.compound_managers.items():
            broker_label = "Deriv" if bkr_name == "deriv" else "Exness"
            has_compound = True

            stats = cm.get_stats()
            current_balance = stats.get("current_balance", 0)
            starting_balance = stats.get("starting_balance", 0)
            peak_balance = stats.get("peak_balance", 0)
            total_trades = stats.get("total_trades", 0)
            win_rate = stats.get("win_rate", 0)

            level_info = cm.get_current_level(current_balance)
            current_level = level_info.get("current", {}).get("label", "?")
            next_level = level_info.get("next", {}).get("label", "Max")
            progress = level_info.get("progress_pct", 0)

            # Barre de progression
            bar_len = 10
            filled = int(progress / 100 * bar_len)
            bar = "\U0001f7e9" * filled + "\u2b1c" * (bar_len - filled)

            pnl_total = current_balance - starting_balance
            roi = (pnl_total / starting_balance * 100) if starting_balance > 0 else 0

            text += (
                "\U0001f310 <b>%s</b>\n"
                "  \U0001f3c6 Niveau: <b>%s</b>\n"
                "  \U0001f3af Prochain: %s\n"
                "  \U0001f4c8 [%s] %.0f%%\n\n"
                "  \U0001f4b0 Solde: %.2f$\n"
                "  \U0001f4c8 Pic: %.2f$\n"
                "  \U0001f4c9 PnL total: %+.2f$ (%+.1f%%)\n"
                "  \U0001f4cb Trades: %d | WR: %.1f%%\n\n"
            ) % (broker_label, current_level, next_level, bar, progress,
                 current_balance, peak_balance, pnl_total, roi, total_trades, win_rate)

        if not has_compound:
            text += "Aucun gestionnaire de croissance composee initialise."
        return text

    def _telegram_performance(self) -> str:
        """Retourne les statistiques detaillees de la session."""
        text = "\U0001f3af <b>PERFORMANCE DETAILLEE</b>\n"
        text += "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"

        uptime = self.cmd_bot.get_session_duration()
        tc = self.telegram._trade_count
        wc = self.telegram._win_count
        lc = self.telegram._loss_count
        wr = (wc / tc * 100) if tc > 0 else 0
        avg_pnl = self.telegram._daily_pnl / tc if tc > 0 else 0

        text += (
            "\U0001f552 Uptime: <b>%s</b>\n"
            "\U0001f504 Cycles: <b>%d</b>\n\n"
            "\U0001f4ca <b>STATISTIQUES QUOTIDIENNES</b>\n"
            "  Trades: <b>%d</b> (%dW / %dL)\n"
            "  Win Rate: <b>%.1f%%</b>\n"
            "  PnL jour: <b>%+.2f$</b>\n"
            "  PnL moyen/trade: <b>%+.2f$</b>\n"
            "  PnL total: <b>%+.2f$</b>\n\n"
            "\U0001f525 <b>SERIES</b>\n"
            "  Serie actuelle: %d\n"
            "  Meilleure serie: %dW\n"
            "  Pire serie: %dL\n\n"
        ) % (uptime, self.cycle_count, tc, wc, lc, wr, self.telegram._daily_pnl, avg_pnl, self.telegram._total_pnl,
             self.telegram._current_streak, self.telegram._best_streak, abs(self.telegram._worst_streak))

        # Metriques par broker
        text += "\U0001f4e1 <b>METRIQUES PAR BROKER</b>\n"
        for bkr_name in self._broker_metrics:
            broker_label = "Deriv" if bkr_name == "deriv" else "MT5"
            text += "  %s: %s\n" % (broker_label, self._get_broker_metrics_summary(bkr_name))

        # Stats dry-run si applicable
        if self.dry_run and self._dry_run_trades:
            dr_wins = sum(1 for t in self._dry_run_trades if t.get("pnl", 0) > 0)
            dr_wr = dr_wins / len(self._dry_run_trades) * 100 if self._dry_run_trades else 0
            text += (
                "\n\U0001f4dd <b>DRY-RUN</b>\n"
                "  Trades simules: <b>%d</b>\n"
                "  PnL simule: <b>%+.2f$</b>\n"
                "  Win Rate simule: <b>%.1f%%</b>"
            ) % (len(self._dry_run_trades), self._dry_run_pnl, dr_wr)

        return text

    def _telegram_clear(self):
        """Remet a zero les statistiques quotidiennes."""
        self.telegram.reset_daily()
        # Remettre a zero les risk managers
        for rm in self.risk_managers.values():
            if hasattr(rm, "reset_daily"):
                rm.reset_daily()
        logger.info("Statistiques quotidiennes remises a zero via Telegram.")

    def _telegram_set_config(self, key: str, value: str) -> str:
        """Modifie une valeur de configuration a chaud."""
        key = key.strip().lower()
        value = value.strip()

        # Mapping des cles vers la config
        key_map = {
            "scan_interval": ("timing", "scan_interval_seconds", int),
            "mode": ("trading", "mode", str),
            "max_positions": ("risk_management", "max_positions", int),
            "default_lot": ("trading", "default_lot_size", float),
            "max_lot": ("trading", "max_lot_size", float),
            "lot_step": ("trading", "lot_step", float),
        }

        if key not in key_map:
            available = ", ".join(key_map.keys())
            return (
                "\u2753 <b>Cle inconnue: %s</b>\n\n"
                "Cles disponibles:\n"
                "  %s"
            ) % (key, available)

        section, config_key, val_type = key_map[key]

        try:
            typed_value = val_type(value)
        except (ValueError, TypeError):
            return "\u274c <b>Valeur invalide</b> pour %s. Attendu: %s" % (key, val_type.__name__)

        # Appliquer la valeur
        if section not in self.config:
            self.config[section] = {}
        old_value = self.config[section].get(config_key, "?")
        self.config[section][config_key] = typed_value

        # Appliquer les effets specifiques
        if key == "scan_interval":
            self.scan_interval = typed_value
            self._build_symbol_scan_intervals()
        elif key == "max_positions":
            for rm in self.risk_managers.values():
                if hasattr(rm, "max_positions"):
                    rm.max_positions = typed_value

        # Sauvegarder dans le fichier de config
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return "\u26a0\ufe0f Valeur appliquee en memoire mais erreur de sauvegarde: %s" % e

        return (
            "\u2705 <b>CONFIG MODIFIEE</b>\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\U0001f4cb Cle: <b>%s</b>\n"
            "\u2b05\ufe0f Ancien: %s\n"
            "\u27a1\ufe0f Nouveau: <b>%s</b>\n"
            "\U0001f4be Sauvegarde dans %s"
        ) % (key, old_value, typed_value, self.config_path)

    def _telegram_restart(self):
        """Redemarre le bot en sauvegardant l'etat."""
        logger = logging.getLogger(__name__)
        logger.info("Redemarrage demande via Telegram.")
        self._save_state()
        logger.info("Etat sauvegarde. Arret pour redemarrage.")
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="Robot Trading v4 - Multi-Broker + Controle Telegram Avance")
    parser.add_argument("--config", type=str, default="config.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--broker", type=str, default=None, choices=["mt5", "deriv", "both"])
    parser.add_argument("--backtest", action="store_true", help="Lance le backtest")
    parser.add_argument("--symbol", type=str, default=None, help="Symbole a backtester")
    parser.add_argument("--days", type=int, default=30, help="Jours de backtest")
    parser.add_argument("--show-trades", action="store_true", help="Afficher chaque trade du backtest")
    parser.add_argument("--report", default="console", choices=["console", "csv", "html"],
                        help="Type de rapport backtest")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print("Erreur : '%s' introuvable." % args.config)
        sys.exit(1)

    if args.backtest:
        from backtester import Backtester
        bt = Backtester(args.config)
        symbols = [args.symbol] if args.symbol else None
        bt.run(symbols=symbols, days=args.days, show_trades=args.show_trades,
               report_type=args.report)
    else:
        bot = TradingBot(config_path=args.config, dry_run=args.dry_run, broker=args.broker)
        bot.start()


if __name__ == "__main__":
    main()
