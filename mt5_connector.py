"""
Gestionnaire de connexion MetaTrader 5.
Gere la connexion, la recuperation des donnees, l'execution des ordres
et le suivi des positions avec reconnexion automatique, verification
de sante et logique de reprises pour les ordres.
"""

import time
import logging
import os
import MetaTrader5 as mt5
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Mapping timeframe MT5
TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

# Mapping type d'ordre (constantes entieres pour compatibilite)
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

# Mapping interne MT5 vers nos constantes
_ORDER_TYPE_TO_MT5 = {
    ORDER_TYPE_BUY: mt5.ORDER_TYPE_BUY,
    ORDER_TYPE_SELL: mt5.ORDER_TYPE_SELL,
}

# Mapping des modes de remplissage
FILLING_MAP = {
    "FOK": mt5.ORDER_FILLING_FOK,
    "IOC": mt5.ORDER_FILLING_IOC,
    "RETURN": mt5.ORDER_FILLING_RETURN,
}

# Parametres de reconnexion
_BACKOFF_INITIAL: float = 1.0
_BACKOFF_MAX: float = 30.0
_BACKOFF_MULTIPLIER: float = 2.0
_RECONNECT_MAX_ATTEMPTS: int = 10
_ORDER_RETRIES: int = 3
_HEALTH_CHECK_INTERVAL: float = 60.0


class MT5Connector:
    """
    Connecteur MetaTrader 5 complet.
    Gere toutes les operations avec le terminal MT5, y compris
    la reconnexion automatique et la verification de sante.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        mt5_cfg = config["mt5"]
        trading_cfg = config["trading"]

        self.login: int = int(os.getenv("MT5_LOGIN", mt5_cfg.get("login", 0)))
        self.password: str = os.getenv("MT5_PASSWORD", mt5_cfg.get("password", ""))
        self.server: str = os.getenv("MT5_SERVER", mt5_cfg.get("server", ""))
        self.mt5_path: str = mt5_cfg["path"]
        self.symbols: List[str] = config.get("brokers", {}).get("mt5", {}).get(
            "symbols", trading_cfg["symbols"]
        )
        self.timeframe_str: str = trading_cfg["timeframe"]
        self.timeframe: int = TIMEFRAME_MAP.get(self.timeframe_str, mt5.TIMEFRAME_M15)
        self.magic_number: int = trading_cfg["magic_number"]
        self.deviation: int = trading_cfg["deviation_slippage"]
        self.history_bars: int = config["timing"]["candle_history_bars"]

        # Etat de connexion
        self.connected: bool = False

        # Reconnexion automatique
        self.reconnect_on_disconnect: bool = True
        self._backoff_current: float = _BACKOFF_INITIAL
        self._reconnect_attempts: int = 0

        # Cache des infos symboles
        self._symbol_info_cache: Dict[str, Any] = {}

        # Suivi des positions (cache local)
        self._position_cache: Dict[int, Dict[str, Any]] = {}
        self._last_health_check: float = 0.0

    # ------------------------------------------------------------------
    #  CONNEXION
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Etablit la connexion a MetaTrader 5 avec reconnexion automatique."""
        self._reconnect_attempts = 0
        self._backoff_current = _BACKOFF_INITIAL

        success = self._try_connect()
        if not success and self.reconnect_on_disconnect:
            return self._auto_reconnect()
        return success

    def _try_connect(self) -> bool:
        """Tente une seule connexion a MT5."""
        # Initialiser MT5
        if not mt5.initialize(path=self.mt5_path):
            err = mt5.last_error()
            logger.error(f"Echec initialisation MT5 : code={err[0]}, message={err[1]}")
            return False

        # Se connecter au compte
        if self.login > 0:
            authorized = mt5.login(
                login=self.login,
                password=self.password,
                server=self.server,
            )
            if not authorized:
                err = mt5.last_error()
                logger.error(f"Echec connexion au compte : code={err[0]}, message={err[1]}")
                logger.error("  -> Verifie vos identifiants login/password/server dans config.json")
                mt5.shutdown()
                return False

        self.connected = True
        self._reconnect_attempts = 0
        self._backoff_current = _BACKOFF_INITIAL
        self._symbol_info_cache.clear()
        self._position_cache.clear()

        # Verifier que les symboles sont disponibles
        for sym in self.symbols:
            if not mt5.symbol_select(sym, True):
                logger.warning(f"Symbole {sym} non disponible dans le Market Watch")

        account = self.get_account_info()
        logger.info(f"Connecte a MT5 | Compte: {account.get('login')} | "
                    f"Serveur: {account.get('server')} | "
                    f"Solde: {account.get('balance')} {account.get('currency')}")
        return True

    def _auto_reconnect(self) -> bool:
        """Boucle de reconnexion avec backoff exponentiel."""
        while self._reconnect_attempts < _RECONNECT_MAX_ATTEMPTS and self.reconnect_on_disconnect:
            attente = self._backoff_current
            self._reconnect_attempts += 1
            logger.info(f"Reconnexion MT5 dans {attente:.1f}s "
                        f"(tentative {self._reconnect_attempts}/{_RECONNECT_MAX_ATTEMPTS})...")
            time.sleep(attente)
            self._backoff_current = min(self._backoff_current * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

            if self._try_connect():
                logger.info("Reconnexion MT5 reussie !")
                return True

        logger.error(f"Impossible de se reconnecter a MT5 apres {_RECONNECT_MAX_ATTEMPTS} tentatives")
        return False

    def disconnect(self) -> None:
        """Deconnecte de MetaTrader 5."""
        self.reconnect_on_disconnect = False
        if self.connected:
            mt5.shutdown()
            self.connected = False
            self._symbol_info_cache.clear()
            self._position_cache.clear()
            logger.info("Deconnecte de MetaTrader 5")

    def is_connected(self) -> bool:
        """Verifie si la connexion est active et le terminal responsive."""
        if not self.connected:
            return False
        # Verifier que le terminal MT5 est toujours responsive
        terminal_info = mt5.terminal_info()
        if terminal_info is None:
            logger.warning("Terminal MT5 non responsive, tentative de reconnexion...")
            if self.reconnect_on_disconnect:
                self._try_reconnect_if_needed()
            return False
        return True

    # ------------------------------------------------------------------
    #  VERIFICATION DE SANTE
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """
        Verifie que le terminal MT5 est operationnel.
        Effectue un test de lecture pour confirmer la reactivite.

        Returns:
            True si le terminal est en bonne sante.
        """
        now = time.time()
        if now - self._last_health_check < 5.0:
            # Eviter les verifications trop frequentes
            return self.connected

        self._last_health_check = now

        # Verifier le terminal
        terminal = mt5.terminal_info()
        if terminal is None:
            logger.error("Verification de sante : terminal MT5 inaccessible")
            self.connected = False
            return False

        # Verifier qu'on peut lire des donnees
        if self.symbols:
            test_sym = self.symbols[0]
            tick = mt5.symbol_info_tick(test_sym)
            if tick is None:
                logger.warning(f"Verification de sante : impossible de lire {test_sym}")
                self.connected = False
                return False

        # Verifier le compte
        account = mt5.account_info()
        if account is None:
            logger.error("Verification de sante : informations de compte inaccessibles")
            self.connected = False
            return False

        return True

    def _try_reconnect_if_needed(self) -> bool:
        """Tente une reconnexion si la connexion est perdue."""
        if self.connected:
            return True
        if not self.reconnect_on_disconnect:
            return False
        logger.warning("Connexion MT5 perdue, tentative de reconnexion...")
        return self._auto_reconnect()

    # ------------------------------------------------------------------
    #  INFORMATIONS DU COMPTE
    # ------------------------------------------------------------------

    def get_account_info(self) -> Dict[str, Any]:
        """Retourne les informations du compte."""
        info = mt5.account_info()
        if info is None:
            logger.warning("Impossible d'obtenir les informations du compte")
            return {}
        return {
            "login": info.login,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "margin_level": info.margin_level,
            "currency": info.currency,
            "server": info.server,
            "leverage": info.leverage,
            "profit": info.profit,
            "trade_mode": info.trade_mode,
            "is_demo": info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
        }

    # ------------------------------------------------------------------
    #  RECUPERATION DES DONNEES DE MARCHE
    # ------------------------------------------------------------------

    def get_ohlc_data(self, symbol: str) -> Optional[Dict[str, np.ndarray]]:
        """
        Recupere les donnees OHLC pour un symbole.

        Returns:
            Dictionnaire avec arrays numpy : open, high, low, close
            ou None si echec.
        """
        rates = mt5.copy_rates_from_pos(symbol, self.timeframe, 0, self.history_bars)
        if rates is None or len(rates) == 0:
            logger.warning(f"Impossible de recuperer les donnees pour {symbol} : {mt5.last_error()}")
            return None

        data: Dict[str, np.ndarray] = {
            "open": np.array(rates["open"], dtype=float),
            "high": np.array(rates["high"], dtype=float),
            "low": np.array(rates["low"], dtype=float),
            "close": np.array(rates["close"], dtype=float),
            "tick_volume": np.array(rates["tick_volume"], dtype=float),
            "time": rates["time"],
        }
        return data

    def get_current_price(self, symbol: str) -> Optional[Tuple[float, float]]:
        """
        Retourne le prix actuel (bid, ask).

        Returns:
            (bid, ask) ou None si echec.
        """
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.warning(f"Impossible d'obtenir les prix pour {symbol}")
            return None
        return tick.bid, tick.ask

    def get_spread(self, symbol: str) -> float:
        """
        Retourne le spread actuel en pips.

        Args:
            symbol: Symbole a analyser.

        Returns:
            Spread en pips. 0.0 si les donnees ne sont pas disponibles.
        """
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            logger.debug(f"Donnees indisponibles pour le spread de {symbol}")
            return 0.0
        spread_points = tick.ask - tick.bid
        pip_size = self._get_pip_size(symbol)
        if pip_size <= 0:
            return 0.0
        return abs(spread_points / pip_size)

    def get_swap_rates(self, symbol: str) -> Dict[str, float]:
        """
        Retourne les taux de swap (overnight) pour un symbole.

        Args:
            symbol: Symbole a analyser.

        Returns:
            Dictionnaire {"long": swap_achat, "short": swap_vente} en devise du compte.
        """
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning(f"Impossible d'obtenir les taux de swap pour {symbol}")
            return {"long": 0.0, "short": 0.0}
        return {
            "long": info.swap_long,
            "short": info.swap_short,
        }

    def is_market_open(self, symbol: str) -> bool:
        """
        Verifie si le marche est ouvert pour un symbole.

        Args:
            symbol: Symbole a verifier.

        Returns:
            True si le marche est ouvert et les transactions sont possibles.
        """
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning(f"Impossible de verifier le marche pour {symbol}")
            return False
        return bool(info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL)

    def get_pip_size(self, symbol: str) -> float:
        """Retourne la taille d'un pip pour le symbole."""
        return self._get_pip_size(symbol)

    def _get_pip_size(self, symbol: str) -> float:
        """Determine la taille d'un pip selon le symbole."""
        profiles = self.config.get("symbol_profiles", {})
        profile = profiles.get(symbol, {})
        if "pip_size" in profile:
            return profile["pip_size"]
        # Essayer de deduire depuis les infos MT5
        info = mt5.symbol_info(symbol)
        if info is not None and info.point > 0:
            # Pour la plupart des symboles, 1 pip = 10 points
            return info.point * 10
        symbol_upper = symbol.upper()
        if "JPY" in symbol_upper:
            return 0.01
        elif "XAU" in symbol_upper or "XAG" in symbol_upper:
            return 0.01
        else:
            return 0.0001

    def get_pip_value_per_lot(self, symbol: str) -> float:
        """Retourne la valeur d'un pip par lot standard pour le symbole."""
        profiles = self.config.get("symbol_profiles", {})
        profile = profiles.get(symbol, {})
        if "pip_value_per_lot" in profile:
            return profile["pip_value_per_lot"]
        symbol_upper = symbol.upper()
        if "JPY" in symbol_upper:
            return 6.5
        elif "XAU" in symbol_upper:
            return 1.0
        else:
            return 10.0

    def _detect_filling_mode(self, symbol: str) -> int:
        """Detecte le mode de remplissage supporte par le broker/symbole."""
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        filling = info.filling_mode
        if filling & 1:
            return mt5.ORDER_FILLING_FOK
        elif filling & 2:
            return mt5.ORDER_FILLING_IOC
        else:
            return mt5.ORDER_FILLING_RETURN

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Retourne les informations detaillees d'un symbole.
        Utilise un cache pour eviter les appels repetes.
        """
        # Verifier le cache
        if symbol in self._symbol_info_cache:
            cached = self._symbol_info_cache[symbol]
            cached_time = cached.get("_cache_time", 0)
            if time.time() - cached_time < 30.0:
                # Retourner une copie sans la metadonnee de cache
                result = {k: v for k, v in cached.items() if not k.startswith("_")}
                return result

        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning(f"Informations indisponibles pour {symbol}")
            return None

        tick = mt5.symbol_info_tick(symbol)
        bid = tick.bid if tick else info.bid
        ask = tick.ask if tick else info.ask

        result: Dict[str, Any] = {
            "name": info.name,
            "bid": bid,
            "ask": ask,
            "point": info.point,
            "digits": info.digits,
            "min_lot": info.volume_min,
            "max_lot": info.volume_max,
            "lot_step": info.volume_step,
            "trade_contract_size": info.trade_contract_size,
            "spread": float(ask - bid) if ask and bid else 0.0,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
            "trade_mode": info.trade_mode,
            "market_open": info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL,
        }

        # Mettre en cache avec horodatage
        result["_cache_time"] = time.time()
        self._symbol_info_cache[symbol] = result

        # Retourner sans la metadonnee
        return {k: v for k, v in result.items() if not k.startswith("_")}

    # ------------------------------------------------------------------
    #  EXECUTION D'ORDRES (avec reprises)
    # ------------------------------------------------------------------

    def open_position(self, symbol: str, order_type: int,
                       lot: float, sl: float, tp: float,
                       comment: str = "") -> Optional[Dict[str, Any]]:
        """
        Ouvre une position sur le marche avec logique de reprises (jusqu'a 3 tentatives).

        Args:
            symbol: Symbole a trader (ex: 'EURUSD')
            order_type: ORDER_TYPE_BUY (0) ou ORDER_TYPE_SELL (1)
            lot: Taille du lot
            sl: Prix du stop-loss
            tp: Prix du take-profit
            comment: Commentaire de l'ordre

        Returns:
            Dict avec resultat de l'ordre ou None si echec total.
        """
        # Verifier que le marche est ouvert
        if not self.is_market_open(symbol):
            logger.error(f"Marche ferme pour {symbol}, ordre annule")
            return None

        for tentative in range(1, _ORDER_RETRIES + 1):
            result = self._send_order(symbol, order_type, lot, sl, tp, comment)

            if result is None:
                logger.warning(f"Envoi echoue pour {symbol} (tentative {tentative}/{_ORDER_RETRIES})")
                if tentative < _ORDER_RETRIES:
                    time.sleep(0.5 * tentative)
                continue

            if result.get("success"):
                return result

            # Analyser le code de retour pour decider si on retente
            retcode = result.get("retcode", -1)
            # Codes non reessayables
            if retcode in (mt5.TRADE_RETCODE_INVALID_FILL, mt5.TRADE_RETCODE_INVALID_VOLUME):
                logger.error(f"Ordre {symbol} rejete de maniere permanente : retcode={retcode}")
                return result
            # Codes reessayables (delai, requote, etc.)
            logger.warning(f"Ordre {symbol} reessaillable : retcode={retcode} "
                           f"(tentative {tentative}/{_ORDER_RETRIES})")
            if tentative < _ORDER_RETRIES:
                time.sleep(0.5 * tentative)
                # Rafraichir les prix avant de retenter
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    logger.error(f"Prix indisponibles pour {symbol} apres echec, abandon")
                    return result

        logger.error(f"Ordre {symbol} definitivement echoue apres {_ORDER_RETRIES} tentatives")
        return result if result else None

    def _send_order(self, symbol: str, order_type: int,
                     lot: float, sl: float, tp: float,
                     comment: str) -> Optional[Dict[str, Any]]:
        """Envoie un seul ordre au marche."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Impossible d'obtenir les prix pour {symbol}")
            return None

        price = tick.ask if order_type == ORDER_TYPE_BUY else tick.bid
        mt5_order_type = _ORDER_TYPE_TO_MT5.get(order_type)
        if mt5_order_type is None:
            logger.error(f"Type d'ordre invalide : {order_type}")
            return None

        filling = self._detect_filling_mode(symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": mt5_order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)

        if result is None:
            err = mt5.last_error()
            logger.error(f"Echec envoi ordre {symbol} : code={err[0]}, message={err[1]}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Ordre rejete {symbol} : retcode={result.retcode}, "
                        f"comment={result.comment}")
            return {
                "success": False,
                "retcode": result.retcode,
                "comment": result.comment,
                "deal": result.deal,
                "order": result.order,
            }

        direction = "ACHAT" if order_type == ORDER_TYPE_BUY else "VENTE"
        logger.info(f"ORDRE EXECUTE [{direction}] {symbol} | "
                    f"Lot={lot} | Prix={price:.5f} | "
                    f"SL={sl:.5f} | TP={tp:.5f} | "
                    f"Ticket={result.order}")

        return {
            "success": True,
            "retcode": result.retcode,
            "comment": result.comment,
            "deal": result.deal,
            "order": result.order,
            "price": price,
            "volume": lot,
            "symbol": symbol,
            "type": order_type,
            "sl": sl,
            "tp": tp,
        }

    def close_position(self, ticket: int) -> bool:
        """
        Ferme une position par son ticket avec reprises.

        Returns:
            True si la fermeture a reussi.
        """
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            logger.warning(f"Position {ticket} non trouvee")
            return False

        pos = position[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error(f"Impossible d'obtenir les prix pour fermer {pos.symbol}")
            return False

        if pos.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        filling = self._detect_filling_mode(pos.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        # Logique de reprises pour la fermeture
        for tentative in range(1, _ORDER_RETRIES + 1):
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Position {ticket} FERMEE | Prix={price:.5f} | PnL={pos.profit:.2f}")
                self._position_cache.pop(ticket, None)
                return True

            logger.warning(f"Fermeture {ticket} echouee (tentative {tentative}/{_ORDER_RETRIES}) : {result}")
            if tentative < _ORDER_RETRIES:
                time.sleep(0.5 * tentative)
                # Rafraichir le prix
                tick = mt5.symbol_info_tick(pos.symbol)
                if tick is None:
                    break
                if pos.type == mt5.POSITION_TYPE_BUY:
                    request["price"] = tick.bid
                else:
                    request["price"] = tick.ask

        logger.error(f"Fermeture position {ticket} definitivement echouee")
        return False

    def close_all_positions(self) -> int:
        """
        Ferme toutes les positions ouvertes par le robot.

        Returns:
            Nombre de positions fermees.
        """
        positions = self.get_bot_positions()
        closed = 0
        for pos in positions:
            if self.close_position(pos["ticket"]):
                closed += 1
        logger.info(f"Fermeture de {closed} positions")
        return closed

    def modify_position_sl(self, ticket: int, new_sl: float) -> bool:
        """
        Modifie le stop-loss d'une position.

        Returns:
            True si la modification a reussi.
        """
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            logger.warning(f"Position {ticket} non trouvee pour modification SL")
            return False

        pos = position[0]

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": new_sl,
            "tp": pos.tp,
        }

        # Reprises pour la modification
        for tentative in range(1, _ORDER_RETRIES + 1):
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"SL modifie position {ticket} : {pos.sl:.5f} -> {new_sl:.5f}")
                # Mettre a jour le cache
                if ticket in self._position_cache:
                    self._position_cache[ticket]["sl"] = new_sl
                return True

            logger.warning(f"Modification SL {ticket} echouee (tentative {tentative}/{_ORDER_RETRIES})")
            if tentative < _ORDER_RETRIES:
                time.sleep(0.5 * tentative)

        logger.error(f"Modification SL position {ticket} definitivement echouee")
        return False

    # ------------------------------------------------------------------
    #  SUIVI DES POSITIONS
    # ------------------------------------------------------------------

    def get_bot_positions(self) -> List[Dict[str, Any]]:
        """
        Retourne toutes les positions ouvertes par le robot (via magic number).
        Ameliore avec un cache local et des informations supplementaires.

        Returns:
            Liste de dictionnaires avec details de chaque position.
        """
        positions = mt5.positions_get()
        if positions is None or len(positions) == 0:
            self._position_cache.clear()
            return []

        bot_positions: List[Dict[str, Any]] = []
        seen_tickets: set = set()

        for pos in positions:
            if pos.magic != self.magic_number:
                continue

            ticket = pos.ticket
            seen_tickets.add(ticket)

            # Conserver les donnees enrichies du cache
            cached = self._position_cache.get(ticket, {})
            entry: Dict[str, Any] = {
                "ticket": ticket,
                "symbol": pos.symbol,
                "type": ORDER_TYPE_BUY if pos.type == mt5.POSITION_TYPE_BUY else ORDER_TYPE_SELL,
                "volume": pos.volume,
                "price_open": pos.price_open,
                "sl": pos.sl,
                "tp": pos.tp,
                "profit": pos.profit,
                "comment": pos.comment,
                "time_open": pos.time_open,
            }

            # Ajouter le spread actuel si disponible
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is not None:
                entry["current_bid"] = tick.bid
                entry["current_ask"] = tick.ask
                entry["spread_pips"] = self.get_spread(pos.symbol)

            # Ajouter les swap rates
            swap = self.get_swap_rates(pos.symbol)
            entry["swap_long"] = swap["long"]
            entry["swap_short"] = swap["short"]

            # Garder les metadonnees du cache (ex: signal d'entree)
            if "signal" in cached:
                entry["signal"] = cached["signal"]

            self._position_cache[ticket] = entry
            bot_positions.append(entry)

        # Nettoyer le cache des positions fermees
        tickets_fermes = [t for t in self._position_cache if t not in seen_tickets]
        for t in tickets_fermes:
            del self._position_cache[t]

        return bot_positions

    def get_position_count(self) -> int:
        """Retourne le nombre de positions ouvertes par le robot."""
        return len(self.get_bot_positions())

    def get_positions_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Retourne les positions du robot pour un symbole donne."""
        return [p for p in self.get_bot_positions() if p["symbol"] == symbol]

    # ------------------------------------------------------------------
    #  HISTORIQUE DES TRADES
    # ------------------------------------------------------------------

    def get_recent_deals(self, days: int = 1) -> List[Dict[str, Any]]:
        """Retourne les deals recents du robot."""
        from_date = datetime.now() - timedelta(days=days)
        deals = mt5.history_deals_get(from_date, datetime.now())

        if deals is None:
            logger.warning(f"Impossible de recuperer l'historique des deals : {mt5.last_error()}")
            return []

        bot_deals: List[Dict[str, Any]] = []
        for deal in deals:
            if deal.magic == self.magic_number and deal.entry == mt5.DEAL_ENTRY_OUT:
                bot_deals.append({
                    "ticket": deal.ticket,
                    "symbol": deal.symbol,
                    "type": ORDER_TYPE_BUY if deal.type == mt5.DEAL_TYPE_BUY else ORDER_TYPE_SELL,
                    "volume": deal.volume,
                    "price": deal.price,
                    "profit": deal.profit,
                    "commission": deal.commission,
                    "swap": deal.swap,
                    "time": deal.time,
                    "comment": deal.comment,
                })
        return bot_deals
