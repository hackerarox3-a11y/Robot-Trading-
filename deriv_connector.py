"""
Connecteur Deriv (deriv.com)
Utilise l'API WebSocket de Deriv pour le trading d'indices synthetiques,
forex et crypto. Meme interface que mt5_connector.py pour compatibilite.

Actifs populaires Deriv :
  - R_75   = Volatility 75 Index
  - R_100  = Volatility 100 Index
  - R_10   = Volatility 10 Index
  - R_25   = Volatility 25 Index
  - R_50   = Volatility 50 Index
  - BOOM1000  = Boom 1000 Index
  - CRASH1000 = Crash 1000 Index
  - frxEURUSD = EUR/USD
  - frxGBPUSD = GBP/USD
  - frxUSDJPY = USD/JPY
  - frxXAUUSD = XAU/USD
"""

import asyncio
import json
import logging
import os
import time
import threading
import numpy as np
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import websockets
except ImportError:
    websockets = None
    logging.warning("Package 'websockets' non installe. pip install websockets")

logger = logging.getLogger(__name__)

# Mapping timeframe Deriv vers secondes
TIMEFRAME_MAP = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

# Parametres de reconnexion avec backoff exponentiel
_BACKOFF_INITIAL: float = 1.0
_BACKOFF_MAX: float = 30.0
_BACKOFF_MULTIPLIER: float = 2.0
_HEARTBEAT_INTERVAL: float = 30.0
_RECONNECT_MAX_ATTEMPTS: int = 20


class _EtatConnexion(Enum):
    """Machine a etats de la connexion WebSocket Deriv."""
    DECONNECTE = auto()
    EN_CONNEXION = auto()
    CONNECTE = auto()
    EN_AUTHENTIFICATION = auto()
    PRET = auto()


class DerivConnector:
    """
    Connecteur Deriv compatible avec l'interface du robot de trading.
    Utilise l'API WebSocket de Deriv avec reconnexion automatique,
    heartbeat, gestion du cycle de vie des contrats et streaming.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        deriv_cfg = config["deriv"]
        trading_cfg = config["trading"]

        self.app_id: int = deriv_cfg["app_id"]
        self.api_token: str = os.getenv("DERIV_API_TOKEN", deriv_cfg.get("api_token", ""))
        self.ws_url: str = deriv_cfg.get("ws_url", "wss://ws.derivws.com/websockets/v3")
        self.symbols: List[str] = config.get("brokers", {}).get("deriv", {}).get(
            "symbols", trading_cfg["symbols"]
        )
        self.timeframe_str: str = trading_cfg["timeframe"]
        self.timeframe_seconds: int = TIMEFRAME_MAP.get(self.timeframe_str, 900)
        self.magic_number: int = trading_cfg["magic_number"]
        self.history_bars: int = config["timing"]["candle_history_bars"]

        # Machine a etats de connexion
        self._etat: _EtatConnexion = _EtatConnexion.DECONNECTE

        # WebSocket
        self.ws: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._msg_id: int = 0
        self._pending_responses: Dict[int, asyncio.Future] = {}
        self._lock: threading.Lock = threading.Lock()

        # Reconnexion automatique
        self.reconnect_on_disconnect: bool = True
        self._backoff_current: float = _BACKOFF_INITIAL
        self._reconnect_attempts: int = 0
        self._listener_task: Optional[asyncio.Task] = None

        # Compte et balance
        self.account_info_cache: Dict[str, Any] = {}

        # Gestion du cycle de vie des contrats
        self._open_contracts: Dict[int, Dict[str, Any]] = {}
        self._closed_contracts: List[Dict[str, Any]] = []

        # Streaming de ticks
        self._tick_callbacks: Dict[str, Callable] = {}
        self._subscribed_symbols: set = set()

        # Heartbeat
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_pong: float = 0.0

    # ------------------------------------------------------------------
    #  Proprietes publiques
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Retourne True si le WebSocket est physiquement connecte."""
        return self._etat in (_EtatConnexion.CONNECTE, _EtatConnexion.EN_AUTHENTIFICATION, _EtatConnexion.PRET)

    @property
    def authorized(self) -> bool:
        """Retourne True si le client est autorise et pret."""
        return self._etat == _EtatConnexion.PRET

    @property
    def etat_str(self) -> str:
        """Retourne l'etat de connexion sous forme de chaine."""
        return self._etat.name

    # ------------------------------------------------------------------
    #  Gestionnaire d'identifiants de messages
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    # ------------------------------------------------------------------
    #  MACHINE A ETATS DE CONNEXION
    # ------------------------------------------------------------------

    def _set_etat(self, nouvel_etat: _EtatConnexion) -> None:
        """Met a jour l'etat de connexion et journalise la transition."""
        ancien = self._etat.name
        self._etat = nouvel_etat
        logger.info(f"Connexion Deriv : {ancien} -> {nouvel_etat.name}")

    # ------------------------------------------------------------------
    #  CONNEXION
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connexion synchronisee via boucle evenementielle."""
        if websockets is None:
            logger.error("Package 'websockets' manquant. pip install websockets")
            return False
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            result = self._loop.run_until_complete(self._async_connect())
            if result:
                # Demarrer le listener de messages en arriere-plan
                self._listener_task = asyncio.ensure_future(
                    self._message_listener(), loop=self._loop
                )
                # Demarrer le heartbeat
                self._heartbeat_task = asyncio.ensure_future(
                    self._heartbeat_loop(), loop=self._loop
                )
            return result
        except Exception as e:
            logger.error(f"Erreur de connexion Deriv : {e}")
            self._set_etat(_EtatConnexion.DECONNECTE)
            return False

    async def _async_connect(self) -> bool:
        """Etablit la connexion WebSocket et l'autorisation."""
        self._set_etat(_EtatConnexion.EN_CONNEXION)

        # Essayer plusieurs app_ids au cas ou le premier echoue
        app_ids_to_try = [self.app_id, 36375, 22574, 1089, 1]
        last_error: Optional[Exception] = None

        for aid in app_ids_to_try:
            try:
                ws_url = f"{self.ws_url}?app_id={aid}"
                logger.info(f"Tentative connexion Deriv avec app_id={aid}...")
                self.ws = await asyncio.wait_for(
                    websockets.connect(ws_url), timeout=15
                )
                logger.info(f"Connecte au serveur Deriv WebSocket (app_id={aid})")
                self.app_id = aid  # garder celui qui marche
                break
            except Exception as e:
                last_error = e
                logger.warning(f"app_id={aid} echoue : {e}")
                continue
        else:
            logger.error(f"Tous les app_ids ont echoue. Derniere erreur : {last_error}")
            self._set_etat(_EtatConnexion.DECONNECTE)
            return False

        self._set_etat(_EtatConnexion.CONNECTE)

        # Autorisation avec le token API
        self._set_etat(_EtatConnexion.EN_AUTHENTIFICATION)
        try:
            await self.ws.send(json.dumps({"authorize": self.api_token}))
            response = await asyncio.wait_for(self.ws.recv(), timeout=10)
            data = json.loads(response)

            if data.get("error"):
                err_msg = data['error'].get('message', data['error'].get('code', 'inconnu'))
                logger.error(f"Autorisation Deriv echouee : {err_msg}")
                logger.error("  -> Verifie ton api_token dans config.json")
                self._set_etat(_EtatConnexion.DECONNECTE)
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                return False

            auth = data.get("authorize", {})
            self.account_info_cache = {
                "login": auth.get("loginid", ""),
                "balance": float(auth.get("balance", 0)),
                "equity": float(auth.get("balance", 0)),
                "currency": auth.get("currency", "USD"),
                "server": "Deriv",
                "leverage": 1,
                "profit": 0,
                "email": auth.get("email", ""),
                "country": auth.get("country", ""),
            }
            self._reconnect_attempts = 0
            self._backoff_current = _BACKOFF_INITIAL
            self._last_pong = time.time()
            self._set_etat(_EtatConnexion.PRET)
            logger.info(f"Autorise Deriv | Compte: {auth.get('loginid')} | "
                        f"Solde: {auth.get('balance')} {auth.get('currency')}")

            # S'abonner aux mises a jour de balance
            await self.ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            logger.info("Abonnement aux mises a jour de balance active")

            return True
        except Exception as e:
            logger.error(f"Erreur d'autorisation Deriv : {e}")
            self._set_etat(_EtatConnexion.DECONNECTE)
            if self.ws:
                await self.ws.close()
                self.ws = None
            return False

    def disconnect(self) -> None:
        """Deconnecte proprement de Deriv."""
        self.reconnect_on_disconnect = False

        # Annuler le listener et le heartbeat
        if self._listener_task and self._loop:
            self._listener_task.cancel()
            self._listener_task = None
        if self._heartbeat_task and self._loop:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self.ws:
            try:
                loop = self._loop or asyncio.new_event_loop()
                if not loop.is_running():
                    loop.run_until_complete(self.ws.close())
                else:
                    asyncio.ensure_future(self.ws.close(), loop=loop)
            except Exception:
                pass
        self.ws = None
        self._set_etat(_EtatConnexion.DECONNECTE)
        self._pending_responses.clear()
        self._subscribed_symbols.clear()
        logger.info("Deconnecte de Deriv")

    def is_connected(self) -> bool:
        """Verifie si la connexion est active et autorisee."""
        return self._etat == _EtatConnexion.PRET

    # ------------------------------------------------------------------
    #  RECONNEXION AUTOMATIQUE AVEC BACKOFF EXPONENTIEL
    # ------------------------------------------------------------------

    def _should_reconnect(self) -> bool:
        """Verifie si une reconnexion est necessaire et autorisee."""
        if not self.reconnect_on_disconnect:
            return False
        if self._reconnect_attempts >= _RECONNECT_MAX_ATTEMPTS:
            logger.error("Nombre maximal de tentatives de reconnexion atteint (%d)",
                         _RECONNECT_MAX_ATTEMPTS)
            return False
        return True

    async def _reconnect(self) -> bool:
        """Tente de se reconnecter avec un delai exponentiel."""
        self._set_etat(_EtatConnexion.DECONNECTE)

        if not self._should_reconnect():
            return False

        attente = self._backoff_current
        self._reconnect_attempts += 1
        logger.info(f"Reconnexion Deriv dans {attente:.1f}s "
                    f"(tentative {self._reconnect_attempts}/{_RECONNECT_MAX_ATTEMPTS})..." )

        # Nettoyer l'ancien listener
        if self._listener_task:
            self._listener_task.cancel()
            self._listener_task = None
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        await asyncio.sleep(attente)
        self._backoff_current = min(self._backoff_current * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

        try:
            ok = await self._async_connect()
            if ok:
                self._listener_task = asyncio.ensure_future(
                    self._message_listener(), loop=self._loop
                )
                self._heartbeat_task = asyncio.ensure_future(
                    self._heartbeat_loop(), loop=self._loop
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Echec de la reconnexion : {e}")
            return False

    # ------------------------------------------------------------------
    #  HEARTBEAT / PING
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Boucle d'envoi periodique de ping pour maintenir la connexion."""
        while self._etat == _EtatConnexion.PRET:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                if self.ws and self._etat == _EtatConnexion.PRET:
                    ping_msg = json.dumps({"ping": 1})
                    await self.ws.send(ping_msg)
                    logger.debug("Heartbeat ping envoye")
                    # Verifier si on a recu un pong recent
                    temps_ecoule = time.time() - self._last_pong
                    if temps_ecoule > _HEARTBEAT_INTERVAL * 3:
                        logger.warning(f"Pas de pong depuis {temps_ecoule:.0f}s, reconnexion..." )
                        await self._reconnect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Erreur heartbeat : {e}")
                try:
                    await self._reconnect()
                except Exception:
                    break

    # ------------------------------------------------------------------
    #  LISTENER DE MESSAGES (arriere-plan)
    # ------------------------------------------------------------------

    async def _message_listener(self) -> None:
        """Ecoute en permanence les messages entrants et les distribue."""
        while self._etat != _EtatConnexion.DECONNECTE:
            try:
                if self.ws is None:
                    await asyncio.sleep(0.5)
                    continue

                raw = await asyncio.wait_for(self.ws.recv(), timeout=60)
                data = json.loads(raw)

                # Repondre aux pongs
                if data.get("msg_type") == "pong":
                    self._last_pong = time.time()
                    continue

                # Distribuer les reponses en attente
                req_id = data.get("req_id")
                if req_id is not None and req_id in self._pending_responses:
                    future = self._pending_responses.pop(req_id)
                    if not future.done():
                        future.set_result(data)
                    continue

                # Mise a jour de balance
                if "balance" in data and "id" not in data:
                    bal = data.get("balance", {})
                    self.account_info_cache["balance"] = float(bal.get("balance", 0))
                    self.account_info_cache["equity"] = float(bal.get("balance", 0))
                    logger.debug(f"Balance mise a jour : {bal.get('balance')} {bal.get('currency')}")
                    continue

                # Mise a jour de contrat (cycle de vie)
                if "proposal_open_contract" in data and "id" not in data:
                    poc = data["proposal_open_contract"]
                    cid = poc.get("contract_id")
                    if cid and cid in self._open_contracts:
                        self._open_contracts[cid]["profit"] = float(poc.get("profit", 0))
                        self._open_contracts[cid]["current_price"] = float(poc.get("current_spot_price", 0))
                        # Verifier si le contrat est termine
                        if poc.get("is_sold") or poc.get("is_expired"):
                            profit = float(poc.get("profit", 0))
                            self._record_closed_contract(cid, profit)
                            del self._open_contracts[cid]
                    continue

                # Streaming de ticks
                if "tick" in data and "id" not in data:
                    tick = data["tick"]
                    sym = tick.get("symbol", "")
                    if sym in self._tick_callbacks:
                        try:
                            self._tick_callbacks[sym]({
                                "symbol": sym,
                                "bid": float(tick.get("quote", 0)),
                                "ask": float(tick.get("quote", 0)),
                                "time": tick.get("epoch", 0),
                            })
                        except Exception as e:
                            logger.debug(f"Erreur callback tick pour {sym} : {e}")
                    continue

            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket ferme inopinement : code={e.code}, raison={e.reason}")
                self._set_etat(_EtatConnexion.DECONNECTE)
                if self._should_reconnect():
                    await self._reconnect()
                else:
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur inattendue dans le listener : {e}")
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    #  COMPTE
    # ------------------------------------------------------------------

    def get_account_info(self) -> Dict[str, Any]:
        """Retourne les informations cachees du compte."""
        return self.account_info_cache

    # ------------------------------------------------------------------
    #  REQUETES
    # ------------------------------------------------------------------

    def _request(self, method: str, **params: Any) -> dict:
        """Envoie une requete WebSocket et attend la reponse (synchrone)."""
        if not self.connected or self.ws is None:
            return {"error": {"message": "Non connecte a Deriv. Veuillez vous reconnecter."}}
        try:
            loop = self._loop or asyncio.new_event_loop()
            if not loop.is_running():
                return loop.run_until_complete(self._async_request(method, **params))
            # Si la boucle tourne deja, on ne peut pas run_until_complete
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self._async_request(method, **params))
                return future.result(timeout=30)
        except Exception as e:
            logger.error(f"Erreur requete {method} : {e}")
            return {"error": {"message": str(e)}}

    async def _async_request(self, method: str, **params: Any) -> dict:
        """Envoie une requete WebSocket asynchrone."""
        req_id = self._next_id()
        msg: Dict[str, Any] = {"req_id": req_id, method: method}
        msg.update(params)

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_responses[req_id] = future

        try:
            await self.ws.send(json.dumps(msg))
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending_responses.pop(req_id, None)
            return {"error": {"message": "Delai d'attente depasse (timeout 30s) pour la requete"}}
        except Exception as e:
            self._pending_responses.pop(req_id, None)
            return {"error": {"message": f"Erreur de communication : {e}"}}

    # ------------------------------------------------------------------
    #  DONNEES DE MARCHE
    # ------------------------------------------------------------------

    def get_ohlc_data(self, symbol: str) -> Optional[Dict[str, np.ndarray]]:
        """Recupere les bougies (candles) pour un symbole."""
        response = self._request(
            "candles",
            ticks_history=symbol,
            adjust_start_time=1,
            count=self.history_bars,
            granularity=self.timeframe_seconds,
            style="candles",
        )
        if response.get("error"):
            logger.warning(f"Impossible d'obtenir candles pour {symbol} : {response['error']}")
            return None

        candles = response.get("candles", [])
        if not candles:
            logger.warning(f"Aucune candle pour {symbol}")
            return None

        opens = np.array([float(c["open"]) for c in candles], dtype=float)
        highs = np.array([float(c["high"]) for c in candles], dtype=float)
        lows = np.array([float(c["low"]) for c in candles], dtype=float)
        closes = np.array([float(c["close"]) for c in candles], dtype=float)
        times = np.array([c["epoch"] for c in candles])

        return {
            "open": opens, "high": highs, "low": lows, "close": closes,
            "tick_volume": np.ones(len(candles)),
            "time": times,
        }

    def get_current_price(self, symbol: str) -> Optional[Tuple[float, float]]:
        """Retourne (bid, ask) ou (price, price) pour Deriv."""
        response = self._request("ticks", ticks=symbol, subscribe=1)
        if response.get("error"):
            return None
        tick = response.get("tick", {})
        if not tick:
            # Essayer avec proposal
            prop = self._request("proposal", contract_type="CALL", symbol=symbol,
                                 duration=5, duration_unit="s", basis="stake",
                                 amount=1, currency="USD")
            if prop.get("error"):
                return None
            ask = float(prop.get("proposal", {}).get("ask_price", 0))
            return (ask, ask * 1.001)  # approximation
        price = float(tick.get("quote", 0))
        pip = self._get_pip_size(symbol)
        return (price, price + pip)

    def get_pip_size(self, symbol: str) -> float:
        """Retourne la taille d'un pip pour le symbole."""
        return self._get_pip_size(symbol)

    def _get_pip_size(self, symbol: str) -> float:
        """Determine la taille d'un pip selon le symbole."""
        profiles = self.config.get("symbol_profiles", {})
        profile = profiles.get(symbol, {})
        if "pip_size" in profile:
            return profile["pip_size"]
        # Indices synthetiques Deriv
        if symbol.startswith("R_") or symbol.startswith("BOOM") or symbol.startswith("CRASH"):
            return 0.01
        # Forex
        if "JPY" in symbol.upper():
            return 0.01
        elif "XAU" in symbol.upper():
            return 0.01
        else:
            return 0.0001

    def get_pip_value_per_lot(self, symbol: str) -> float:
        """Retourne la valeur d'un pip par lot standard."""
        profiles = self.config.get("symbol_profiles", {})
        profile = profiles.get(symbol, {})
        if "pip_value_per_lot" in profile:
            return profile["pip_value_per_lot"]
        if symbol.startswith("R_") or symbol.startswith("BOOM") or symbol.startswith("CRASH"):
            return 1.0
        return 10.0

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retourne les informations detaillees d'un symbole."""
        response = self._request("trading_times", symbols=json.dumps([symbol]))
        if response.get("error"):
            logger.warning(f"Impossible d'obtenir les infos pour {symbol} : {response['error']}")
            return None
        times = response.get("trading_times", {}).get(symbol, {})
        prices = self.get_current_price(symbol)
        return {
            "name": symbol,
            "bid": prices[0] if prices else 0,
            "ask": prices[1] if prices else 0,
            "min_lot": 0.35,
            "max_lot": 100.0,
            "lot_step": 0.35,
            "trading_times": times,
        }

    # ------------------------------------------------------------------
    #  PROPOSAL ET PAYOUT
    # ------------------------------------------------------------------

    def get_payout(self, symbol: str, order_type: int, stake: float,
                   duration: Optional[int] = None, duration_unit: Optional[str] = None
                   ) -> Optional[Dict[str, Any]]:
        """
        Demande un proposal avant d'acheter pour connaitre le payout reel.

        Args:
            symbol: Symbole a trader.
            order_type: ORDER_TYPE_BUY (CALL) ou ORDER_TYPE_SELL (PUT).
            stake: Mise en monnaie du compte.
            duration: Duree du contrat (defaut selon timeframe).
            duration_unit: Unite de duree (defaut "m" = minutes).

        Returns:
            Dictionnaire avec ask_price, payout, id de proposal ou None si echec.
        """
        contract_type = "CALL" if order_type == ORDER_TYPE_BUY else "PUT"
        if duration is None:
            duree_map = {"M1": 1, "M5": 3, "M15": 5, "M30": 10, "H1": 30, "H4": 120, "D1": 480}
            duration = duree_map.get(self.timeframe_str, 5)
        if duration_unit is None:
            duration_unit = "m"

        response = self._request(
            "proposal",
            contract_type=contract_type,
            symbol=symbol,
            duration=duration,
            duration_unit=duration_unit,
            basis="stake",
            amount=stake,
            currency=self.account_info_cache.get("currency", "USD"),
        )
        if response.get("error"):
            logger.warning(f"Impossible d'obtenir le proposal pour {symbol} : {response['error']}")
            return None

        prop = response.get("proposal", {})
        ask_price = float(prop.get("ask_price", 0))
        payout = float(prop.get("payout", 0))
        id_proposal = prop.get("id", "")

        ratio = (payout / ask_price * 100) if ask_price > 0 else 0
        logger.info(f"Proposal {contract_type} {symbol} | Mise={stake} | "
                    f"Prix={ask_price:.4f} | Payout={payout:.2f} | Ratio={ratio:.1f}%")

        return {
            "ask_price": ask_price,
            "payout": payout,
            "payout_ratio": ratio,
            "id": id_proposal,
            "contract_type": contract_type,
        }

    # ------------------------------------------------------------------
    #  EXECUTION D'ORDRES
    # ------------------------------------------------------------------

    def open_position(self, symbol: str, order_type: int,
                       lot: float, sl: float, tp: float,
                       comment: str = "") -> Optional[Dict[str, Any]]:
        """
        Ouvre un contrat sur Deriv.
        Pour les indices synthetiques, utilise CALL/PUT.
        Pour les forex, utilise les contrats vanille.
        Demande un proposal avant l'achat pour obtenir le prix reel.
        """
        # Obtenir le payout avant d'acheter
        proposal = self.get_payout(symbol, order_type, lot)
        if proposal is None:
            logger.error(f"Impossible d'obtenir le proposal pour {symbol}, achat annule")
            return None

        is_synthetic = symbol.startswith("R_") or symbol.startswith("BOOM") or symbol.startswith("CRASH")

        if is_synthetic:
            return self._buy_synthetic(symbol, order_type, lot, sl, tp, comment, proposal)
        else:
            return self._buy_vanilla(symbol, order_type, lot, sl, tp, proposal, comment)

    def _buy_synthetic(self, symbol: str, order_type: int,
                        stake: float, sl: float, tp: float,
                        comment: str, proposal: Dict[str, Any]
                        ) -> Optional[Dict[str, Any]]:
        """Achete un contrat synthetique (CALL/PUT) sur Deriv apres proposal."""
        contract_type = "CALL" if order_type == ORDER_TYPE_BUY else "PUT"

        duree_map = {"M1": 1, "M5": 3, "M15": 5, "M30": 10, "H1": 30, "H4": 120, "D1": 480}
        duration = duree_map.get(self.timeframe_str, 5)

        params = {
            "contract_type": contract_type,
            "symbol": symbol,
            "duration": duration,
            "duration_unit": "m",
            "basis": "stake",
            "amount": stake,
            "currency": self.account_info_cache.get("currency", "USD"),
        }

        response = self._request(
            "buy",
            buy=proposal.get("id", 1),
            subscribe=1,
            parameters=json.dumps(params),
        )

        if response.get("error"):
            logger.error(f"Echec achat {symbol} : {response['error']['message']}")
            return None

        buy = response.get("buy", {})
        contract_id = buy.get("contract_id", 0)
        buy_price = float(buy.get("buy_price", 0))
        longcode = buy.get("longcode", "")
        payout = float(buy.get("payout", 0))

        logger.info(f"CONTRAT OUVERT [{contract_type}] {symbol} | "
                    f"Mise={stake} | Prix={buy_price} | Payout={payout} | ID={contract_id} | {longcode}")

        self._open_contracts[contract_id] = {
            "contract_id": contract_id,
            "symbol": symbol,
            "type": order_type,
            "volume": stake,
            "price_open": buy_price,
            "payout": payout,
            "sl": sl,
            "tp": tp,
            "profit": 0.0,
            "comment": comment,
            "time_open": datetime.now().timestamp(),
            "longcode": longcode,
            "contract_type": contract_type,
            "is_sold": False,
            "is_expired": False,
        }

        return {
            "success": True,
            "retcode": 0,
            "comment": longcode,
            "deal": contract_id,
            "order": contract_id,
            "price": buy_price,
            "volume": stake,
            "symbol": symbol,
            "type": order_type,
            "sl": sl,
            "tp": tp,
        }

    def _buy_vanilla(self, symbol: str, order_type: int,
                      lot: float, sl: float, tp: float,
                      proposal: Dict[str, Any], comment: str
                      ) -> Optional[Dict[str, Any]]:
        """Achete un contrat forex vanille sur Deriv apres proposal."""
        contract_type = "CALL" if order_type == ORDER_TYPE_BUY else "PUT"

        prices = self.get_current_price(symbol)
        entry_price = prices[1] if prices else proposal.get("ask_price", 0)
        pip = self._get_pip_size(symbol)

        if order_type == ORDER_TYPE_BUY:
            barrier = entry_price - (abs(entry_price - sl) / pip) * pip * 0.5
        else:
            barrier = entry_price + (abs(tp - entry_price) / pip) * pip * 0.5

        params = {
            "contract_type": contract_type,
            "symbol": symbol,
            "duration": 5,
            "duration_unit": "m",
            "basis": "stake",
            "amount": lot,
            "currency": self.account_info_cache.get("currency", "USD"),
            "barrier": round(barrier, 4),
        }

        response = self._request(
            "buy",
            buy=proposal.get("id", 1),
            subscribe=1,
            parameters=json.dumps(params),
        )

        if response.get("error"):
            logger.error(f"Echec achat vanille {symbol} : {response['error']['message']}")
            return None

        buy = response.get("buy", {})
        contract_id = buy.get("contract_id", 0)
        buy_price = float(buy.get("buy_price", 0))
        longcode = buy.get("longcode", "")

        logger.info(f"CONTRAT VANILLE [{contract_type}] {symbol} | "
                    f"Mise={lot} | Prix={buy_price} | ID={contract_id}")

        self._open_contracts[contract_id] = {
            "contract_id": contract_id,
            "symbol": symbol,
            "type": order_type,
            "volume": lot,
            "price_open": buy_price,
            "payout": 0.0,
            "sl": sl,
            "tp": tp,
            "profit": 0.0,
            "comment": comment,
            "time_open": datetime.now().timestamp(),
            "longcode": longcode,
            "contract_type": contract_type,
            "is_sold": False,
            "is_expired": False,
        }

        return {
            "success": True, "retcode": 0, "deal": contract_id,
            "order": contract_id, "price": buy_price, "volume": lot,
            "symbol": symbol, "type": order_type, "sl": sl, "tp": tp,
        }

    def close_position(self, ticket: int) -> bool:
        """Ferme un contrat (sell back) sur Deriv."""
        response = self._request("sell", contract_id=ticket, price=100)
        if response.get("error"):
            logger.error(f"Echec fermeture contrat {ticket} : {response['error']}")
            return False
        sell = response.get("sell", {})
        sell_price = float(sell.get("sell_price", 0))
        buy_price = self._open_contracts.get(ticket, {}).get("price_open", 0)
        pnl = sell_price - buy_price
        logger.info(f"Contrat {ticket} FERMEE | Prix vente={sell_price:.4f} | PnL={pnl:.2f}")
        self._record_closed_contract(ticket, pnl)
        self._open_contracts.pop(ticket, None)
        return True

    def cancel_contract(self, ticket: int) -> bool:
        """
        Annule/revend un contrat avant son expiration (sell back).
        Retourne True si l'operation a reussi.
        """
        if ticket not in self._open_contracts:
            logger.warning(f"Contrat {ticket} non trouve dans les contrats ouverts")
            return False

        # Obtenir le prix de rachat actuel
        response = self._request(
            "proposal_open_contract",
            contract_id=ticket,
            sell_price=1,
        )
        if response.get("error"):
            logger.error(f"Impossible d'obtenir le prix de rachat pour {ticket} : {response['error']}")
            return False

        poc = response.get("proposal_open_contract", {})
        sell_price = float(poc.get("sell_price", 0))
        if sell_price <= 0:
            logger.warning(f"Prix de rachat invalide pour le contrat {ticket} : {sell_price}")
            return False

        # Effectuer la vente anticipee
        sell_resp = self._request("sell", contract_id=ticket, price=sell_price)
        if sell_resp.get("error"):
            logger.error(f"Echec de la vente anticipee du contrat {ticket} : {sell_resp['error']}")
            return False

        buy_price = self._open_contracts[ticket].get("price_open", 0)
        pnl = sell_price - buy_price
        logger.info(f"Contrat {ticket} ANNULE (vente anticipee) | "
                    f"Prix achat={buy_price:.4f} | Prix vente={sell_price:.4f} | PnL={pnl:.2f}")
        self._record_closed_contract(ticket, pnl)
        del self._open_contracts[ticket]
        return True

    def close_all_positions(self) -> int:
        """Ferme tous les contrats ouverts. Retourne le nombre ferme."""
        closed = 0
        for cid in list(self._open_contracts.keys()):
            if self.cancel_contract(cid):
                closed += 1
        logger.info(f"Fermeture de {closed} contrat(s)")
        return closed

    def modify_position_sl(self, ticket: int, new_sl: float) -> bool:
        """Deriv ne permet pas de modifier le SL directement, stocke en local."""
        if ticket in self._open_contracts:
            self._open_contracts[ticket]["sl"] = new_sl
            logger.info(f"SL mis a jour localement pour le contrat {ticket} : {new_sl}")
            return True
        logger.warning(f"Contrat {ticket} non trouve pour modification du SL")
        return False

    # ------------------------------------------------------------------
    #  CYCLE DE VIE DES CONTRATS
    # ------------------------------------------------------------------

    def get_contract(self, contract_id: int) -> Optional[Dict[str, Any]]:
        """Retourne les details d'un contrat ouvert ou None."""
        if contract_id in self._open_contracts:
            return self._open_contracts[contract_id]
        return None

    def get_all_contracts(self) -> Dict[int, Dict[str, Any]]:
        """Retourne tous les contrats ouverts."""
        return dict(self._open_contracts)

    def get_closed_contracts(self) -> List[Dict[str, Any]]:
        """Retourne l'historique des contrats fermes durant cette session."""
        return list(self._closed_contracts)

    def _record_closed_contract(self, contract_id: int, profit: float) -> None:
        """Enregistre un contrat ferme dans l'historique."""
        contract = self._open_contracts.pop(contract_id, {})
        contract["profit_final"] = profit
        contract["time_closed"] = datetime.now().timestamp()
        self._closed_contracts.append(contract)
        logger.info(f"Contrat {contract_id} ferme | PnL={profit:.2f}")

    # ------------------------------------------------------------------
    #  SUIVI DES POSITIONS (batch)
    # ------------------------------------------------------------------

    def get_bot_positions(self) -> List[Dict[str, Any]]:
        """
        Retourne les contrats ouverts par le robot.
        Utilise une verification par lot pour plus d'efficacite.
        """
        if not self._open_contracts:
            return []

        # Verifier par lot si les contrats sont encore ouverts
        contract_ids = list(self._open_contracts.keys())
        if not contract_ids:
            return []

        open_positions: List[Dict[str, Any]] = []

        # Utiliser profit_table avec filtrage pour verification par lot
        response = self._request(
            "profit_table",
            description=1,
            limit=50,
            sort="DESC",
        )

        if response.get("error"):
            logger.warning(f"Impossible de verifier les contrats : {response['error']}")
            # Retourner les contrats locaux comme fallback
            return list(self._open_contracts.values())

        # Construire un ensemble des IDs de contrats actifs
        actifs = set()
        for row in response.get("profit_table", {}).get("history", []):
            cid = row.get("contract_id")
            if cid:
                actifs.add(cid)

        # Verifier chaque contrat local
        for cid, contract in list(self._open_contracts.items()):
            # Verifier individuellement seulement si pas dans la table
            poc_resp = self._request(
                "proposal_open_contract",
                contract_id=cid,
                subscribe=0,
            )
            if poc_resp.get("error"):
                self._open_contracts.pop(cid, None)
                continue

            poc = poc_resp.get("proposal_open_contract", {})
            if poc.get("is_sold") or poc.get("is_expired"):
                profit = float(poc.get("profit", 0))
                self._record_closed_contract(cid, profit)
                continue

            # Mettre a jour les donnees du contrat
            contract["profit"] = float(poc.get("profit", 0))
            contract["current_price"] = float(poc.get("current_spot_price", 0))
            contract["bid_price"] = float(poc.get("bid_price", 0))
            open_positions.append(contract)

        return open_positions

    def get_position_count(self) -> int:
        """Retourne le nombre de positions ouvertes par le robot."""
        return len(self.get_bot_positions())

    def get_positions_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Retourne les positions du robot pour un symbole donne."""
        return [p for p in self.get_bot_positions() if p["symbol"] == symbol]

    # ------------------------------------------------------------------
    #  PROFIT & LOSS
    # ------------------------------------------------------------------

    def get_pl(self) -> float:
        """
        Retourne le P&L total de tous les contrats ouverts.
        """
        positions = self.get_bot_positions()
        total_pl = sum(float(p.get("profit", 0)) for p in positions)
        return total_pl

    # ------------------------------------------------------------------
    #  STREAMING DE TICKS
    # ------------------------------------------------------------------

    def subscribe_ticks(self, symbol: str, callback: Callable[[Dict[str, Any]], None]) -> bool:
        """
        Abonne un callback aux ticks en temps reel pour un symbole.

        Args:
            symbol: Symbole a surveiller.
            callback: Fonction appelee a chaque tick avec un dictionnaire {symbol, bid, ask, time}.

        Returns:
            True si l'abonnement a reussi.
        """
        if not self.is_connected():
            logger.error("Impossible de s'abonner aux ticks : non connecte")
            return False

        self._tick_callbacks[symbol] = callback
        self._subscribed_symbols.add(symbol)

        response = self._request("ticks", ticks=symbol, subscribe=1)
        if response.get("error"):
            logger.warning(f"Echec abonnement ticks pour {symbol} : {response['error']}")
            return False

        logger.info(f"Abonnement ticks actif pour {symbol}")
        return True

    def unsubscribe_ticks(self, symbol: str) -> bool:
        """Desabonne un symbole du streaming de ticks."""
        self._tick_callbacks.pop(symbol, None)
        self._subscribed_symbols.discard(symbol)

        response = self._request("forget", ticks=symbol)
        if response.get("error"):
            logger.warning(f"Echec desabonnement ticks pour {symbol} : {response['error']}")
            return False

        logger.info(f"Desabonnement ticks pour {symbol}")
        return True

    def forget_all(self) -> None:
        """Oublie tous les abonnements en cours (ticks, contrats)."""
        self._request("forget_all")
        self._tick_callbacks.clear()
        self._subscribed_symbols.clear()
        logger.info("Tous les abonnements oubles")

    # ------------------------------------------------------------------
    #  HISTORIQUE
    # ------------------------------------------------------------------

    def get_recent_deals(self, days: int = 1) -> List[Dict[str, Any]]:
        """Retourne les deals recents."""
        response = self._request(
            "profit_table",
            description=1,
            limit=50,
            sort="ASC",
            date_start=int((datetime.now() - timedelta(days=days)).timestamp()),
        )
        deals: List[Dict[str, Any]] = []
        for row in response.get("profit_table", {}).get("history", []):
            deals.append({
                "ticket": row.get("contract_id", 0),
                "symbol": row.get("shortcode", "").split("_")[0],
                "type": ORDER_TYPE_BUY if "CALL" in str(row.get("shortcode", "")) else ORDER_TYPE_SELL,
                "volume": float(row.get("buy_price", 0)),
                "price": float(row.get("sell_price", 0)),
                "profit": float(row.get("profit", 0)),
                "commission": 0,
                "swap": 0,
                "time": row.get("purchase_time", 0),
                "comment": row.get("longcode", ""),
            })
        return deals
