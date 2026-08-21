"""
Filtre d'actualites economiques v3.
======================================================
Ameliorations v3 :
  1. Recuperation automatique depuis le web (ForexFactory RSS + sources de secours)
  2. Mapping NEWS -> SYMBOLES (USD, EUR, GBP, JPY, CHF, Or, global)
  3. Cache intelligent avec rafraichissement adaptatif (plus frequent pres des news)
  4. Mode "avertir seul" (ne bloque pas, informe seulement)
  5. Evenements economiques elargis : NFP, CPI, PPI, PMI, FOMC, Emploi,
     Ventes au detail, Balance commerciale, Confiance consommation, Logement,
     Procès-verbaux FOMC, Discours banques centrales
  6. Zones d'evitement PRE-NEWS et POST-NEWS configurables separement
  7. Whitelist/Blacklist de symboles
  8. Score d'impact historique (1-5) base sur la volatilite
  9. Analyse de la force des devises pendant les evenements
 10. Analyse du sentiment (meilleur/pire que prevu = impact plus fort)
 11. Sources web multiples avec fallback
 12. get_upcoming_impact() : impact agregé pour les N prochaines heures

Fonctionnement :
  - Priorite 1: News depuis le web (si connexion dispo)
  - Priorite 2: Calendrier interne (fallback hors ligne)
  - Chaque news est associee a des symboles affectes
  - Si le symbole qu'on veut trader est affecte par une news proche,
    le trade est bloque ou la mise est reduite

Mapping des symboles :
  - News USD -> XAUUSD, EURUSD, GBPUSD, USDJPY, USDCHF, R_75, R_100
  - News EUR -> EURUSD, frxEURUSD
  - News GBP -> GBPUSD
  - News JPY -> USDJPY
  - News CHF -> USDCHF
  - News Or (Gold) -> XAUUSD
  - News globales -> tous les indices synthetiques
"""

import logging
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Mapping des devises aux symboles du robot
CURRENCY_SYMBOL_MAP = {
    "USD": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "R_75", "R_100", "BOOM1000", "CRASH1000", "frxEURUSD"],
    "EUR": ["EURUSD", "frxEURUSD"],
    "GBP": ["GBPUSD"],
    "JPY": ["USDJPY"],
    "CHF": ["USDCHF"],
    "XAU": ["XAUUSD"],
    "GOLD": ["XAUUSD"],
    "GLOBAL": ["R_75", "R_100", "BOOM1000", "CRASH1000"],
}

# Symboles qui sont toujours sensibles aux news globales
ALWAYS_SENSITIVE = ["XAUUSD", "R_75", "R_100", "BOOM1000", "CRASH1000"]

# Sources web avec fallback (dans l'ordre de priorite)
WEB_NEWS_SOURCES = [
    {
        "name": "ForexFactory RSS",
        "url": "https://www.forexfactory.com/rss/calendar",
        "type": "rss",
    },
    {
        "name": "Investing.com Calendar",
        "url": "https://www.investing.com/economic-calendar/",
        "type": "html",
        "fallback_only": True,
    },
]

# Force historique des devises par evenement (basee sur la volatilite observee)
CURRENCY_STRENGTH_BY_EVENT = {
    "NFP": {"USD": 5.0},
    "CPI_US": {"USD": 4.5},
    "CPI_EU": {"EUR": 3.5},
    "FOMC": {"USD": 5.0, "GLOBAL": 4.0},
    "GDP_US": {"USD": 3.5},
    "RATE_DECISION_US": {"USD": 5.0},
    "RETAIL_SALES": {"USD": 3.0},
    "PMI_MANUFACTURING": {"USD": 2.5, "GLOBAL": 2.0},
    "PMI_SERVICES": {"USD": 2.5, "GLOBAL": 2.0},
    "JOBLESS_CLAIMS": {"USD": 2.0},
    "UNEMPLOYMENT_RATE": {"USD": 3.5},
    "EMPLOYMENT_SITUATION": {"USD": 4.0},
    "TRADE_BALANCE": {"USD": 2.0},
    "CONSUMER_CONFIDENCE": {"USD": 2.5},
    "HOUSING_STARTS": {"USD": 2.5},
    "EXISTING_HOME_SALES": {"USD": 2.0},
    "PPI": {"USD": 3.0},
    "FOMC_MINUTES": {"USD": 4.0},
    "FED_SPEECH": {"USD": 3.0},
    "ECB_RATE": {"EUR": 4.0},
    "BOE_RATE": {"GBP": 3.5},
}


class NewsFilter:
    """Filtre les periodes a haut risque autour des news economiques."""

    # Echelle d'impact etendue (1-5)
    IMPACT_EXTREME = 5
    IMPACT_TRES_HIGH = 4
    HIGH = 3
    MEDIUM = 2
    LOW = 1

    IMPACT_LABELS = {
        5: "EXTREME", 4: "TRES HAUT", 3: "HAUT",
        2: "MOYEN", 1: "FAIBLE",
    }

    RECURRING_EVENTS = {
        "NFP": {
            "type": "first_friday",
            "hour_utc": 12, "minute_utc": 30,
            "impact": HIGH,
            "avoid_before_min": 120, "avoid_after_min": 60,
            "currencies": ["USD", "GLOBAL"],
            "description": "Non-Farm Payrolls",
            "historical_impact": 5,
        },
        "CPI_US": {
            "type": "monthly", "day_offset": 12,
            "hour_utc": 13, "minute_utc": 30,
            "impact": HIGH,
            "avoid_before_min": 60, "avoid_after_min": 30,
            "currencies": ["USD", "GLOBAL"],
            "description": "US Consumer Price Index (CPI m/m)",
            "historical_impact": 5,
        },
        "CPI_EU": {
            "type": "monthly", "day_offset": 17,
            "hour_utc": 10, "minute_utc": 0,
            "impact": MEDIUM,
            "avoid_before_min": 45, "avoid_after_min": 15,
            "currencies": ["EUR"],
            "description": "EU Consumer Price Index",
            "historical_impact": 3,
        },
        "FOMC": {
            "type": "fomc_dates",
            "dates": [
                (2025, 1, 29), (2025, 3, 19), (2025, 5, 7),
                (2025, 6, 18), (2025, 7, 30), (2025, 9, 17),
                (2025, 11, 5), (2025, 12, 17),
                (2026, 1, 28), (2026, 3, 18), (2026, 5, 6),
                (2026, 6, 17), (2026, 7, 29), (2026, 9, 16),
                (2026, 11, 4), (2026, 12, 16),
            ],
            "hour_utc": 18, "minute_utc": 0,
            "impact": HIGH,
            "avoid_before_min": 120, "avoid_after_min": 60,
            "currencies": ["USD", "GLOBAL"],
            "description": "FOMC Decision des Taux d'Interet",
            "historical_impact": 5,
        },
        "FOMC_MINUTES": {
            "type": "fomc_minutes",
            "dates": [
                (2025, 2, 19), (2025, 4, 9), (2025, 6, 11),
                (2025, 7, 9), (2025, 9, 10), (2025, 11, 12),
                (2026, 2, 18), (2026, 4, 8), (2026, 6, 10),
            ],
            "hour_utc": 19, "minute_utc": 0,
            "impact": IMPACT_TRES_HIGH,
            "avoid_before_min": 60, "avoid_after_min": 30,
            "currencies": ["USD", "GLOBAL"],
            "description": "FOMC Proces-Verbaux (Minutes)",
            "historical_impact": 4,
        },
        "GDP_US": {
            "type": "quarterly", "months": [1, 4, 7, 10], "day_offset": 25,
            "hour_utc": 12, "minute_utc": 30,
            "impact": MEDIUM,
            "avoid_before_min": 60, "avoid_after_min": 30,
            "currencies": ["USD", "GLOBAL"],
            "description": "US PIB (GDP)",
            "historical_impact": 4,
        },
        "RATE_DECISION_US": {
            "type": "fomc_dates",
            "dates": [
                (2025, 1, 29), (2025, 3, 19), (2025, 5, 7),
                (2025, 6, 18), (2025, 7, 30), (2025, 9, 17),
                (2025, 11, 5), (2025, 12, 17),
                (2026, 1, 28), (2026, 3, 18), (2026, 5, 6),
                (2026, 6, 17), (2026, 7, 29), (2026, 9, 16),
                (2026, 11, 4), (2026, 12, 16),
            ],
            "hour_utc": 18, "minute_utc": 0,
            "impact": HIGH,
            "avoid_before_min": 90, "avoid_after_min": 45,
            "currencies": ["USD"],
            "description": "US Decision des Taux d'Interet",
            "historical_impact": 5,
        },
        "RETAIL_SALES": {
            "type": "monthly", "day_offset": 15,
            "hour_utc": 12, "minute_utc": 30,
            "impact": MEDIUM,
            "avoid_before_min": 30, "avoid_after_min": 15,
            "currencies": ["USD"],
            "description": "US Ventes au Detail (Retail Sales)",
            "historical_impact": 3,
        },
        "PMI_MANUFACTURING": {
            "type": "monthly", "day_offset": 1,
            "hour_utc": 13, "minute_utc": 45,
            "impact": MEDIUM,
            "avoid_before_min": 30, "avoid_after_min": 15,
            "currencies": ["USD", "GLOBAL"],
            "description": "US PMI Manufacturier",
            "historical_impact": 3,
        },
        "PMI_SERVICES": {
            "type": "monthly", "day_offset": 5,
            "hour_utc": 13, "minute_utc": 45,
            "impact": MEDIUM,
            "avoid_before_min": 30, "avoid_after_min": 15,
            "currencies": ["USD", "GLOBAL"],
            "description": "US PMI Services",
            "historical_impact": 3,
        },
        "JOBLESS_CLAIMS": {
            "type": "weekly_thursday",
            "hour_utc": 12, "minute_utc": 30,
            "impact": LOW,
            "avoid_before_min": 15, "avoid_after_min": 5,
            "currencies": ["USD"],
            "description": "US Demandes d'Allocation Chomage",
            "historical_impact": 2,
        },
        "UNEMPLOYMENT_RATE": {
            "type": "monthly", "day_offset": 3,
            "hour_utc": 12, "minute_utc": 30,
            "impact": MEDIUM,
            "avoid_before_min": 45, "avoid_after_min": 15,
            "currencies": ["USD"],
            "description": "US Taux de Chomage",
            "historical_impact": 3,
        },
        "EMPLOYMENT_SITUATION": {
            "type": "first_friday",
            "hour_utc": 12, "minute_utc": 30,
            "impact": HIGH,
            "avoid_before_min": 120, "avoid_after_min": 60,
            "currencies": ["USD", "GLOBAL"],
            "description": "US Situation de l'Emploi (Emploi + Chomage)",
            "historical_impact": 5,
        },
        "TRADE_BALANCE": {
            "type": "monthly", "day_offset": 7,
            "hour_utc": 8, "minute_utc": 30,
            "impact": MEDIUM,
            "avoid_before_min": 20, "avoid_after_min": 10,
            "currencies": ["USD"],
            "description": "US Balance Commerciale (Trade Balance)",
            "historical_impact": 2,
        },
        "CONSUMER_CONFIDENCE": {
            "type": "monthly", "day_offset": 25,
            "hour_utc": 14, "minute_utc": 0,
            "impact": MEDIUM,
            "avoid_before_min": 20, "avoid_after_min": 10,
            "currencies": ["USD"],
            "description": "US Confiance des Consommateurs",
            "historical_impact": 3,
        },
        "HOUSING_STARTS": {
            "type": "monthly", "day_offset": 17,
            "hour_utc": 12, "minute_utc": 30,
            "impact": MEDIUM,
            "avoid_before_min": 20, "avoid_after_min": 10,
            "currencies": ["USD"],
            "description": "US Mises en Chantier (Housing Starts)",
            "historical_impact": 3,
        },
        "EXISTING_HOME_SALES": {
            "type": "monthly", "day_offset": 22,
            "hour_utc": 14, "minute_utc": 0,
            "impact": MEDIUM,
            "avoid_before_min": 20, "avoid_after_min": 10,
            "currencies": ["USD"],
            "description": "US Ventes de Logements Existant",
            "historical_impact": 2,
        },
        "PPI": {
            "type": "monthly", "day_offset": 14,
            "hour_utc": 12, "minute_utc": 30,
            "impact": MEDIUM,
            "avoid_before_min": 30, "avoid_after_min": 15,
            "currencies": ["USD"],
            "description": "US Index des Prix a la Production (PPI)",
            "historical_impact": 3,
        },
        "FED_SPEECH": {
            "type": "weekly_varies",
            "hour_utc": 14, "minute_utc": 0,
            "impact": MEDIUM,
            "avoid_before_min": 15, "avoid_after_min": 10,
            "currencies": ["USD"],
            "description": "Discours Membre FED",
            "historical_impact": 3,
        },
        "ECB_RATE": {
            "type": "ecb_dates",
            "dates": [
                (2025, 1, 30), (2025, 3, 6), (2025, 4, 10),
                (2025, 6, 5), (2025, 7, 17), (2025, 9, 11),
                (2025, 10, 16), (2025, 12, 11),
                (2026, 1, 22), (2026, 3, 12), (2026, 5, 7),
            ],
            "hour_utc": 12, "minute_utc": 45,
            "impact": HIGH,
            "avoid_before_min": 60, "avoid_after_min": 30,
            "currencies": ["EUR"],
            "description": "BCE Decision des Taux",
            "historical_impact": 4,
        },
        "BOE_RATE": {
            "type": "boe_dates",
            "dates": [
                (2025, 2, 6), (2025, 3, 20), (2025, 5, 8),
                (2025, 6, 19), (2025, 8, 7), (2025, 9, 18),
                (2025, 11, 6), (2025, 12, 18),
            ],
            "hour_utc": 12, "minute_utc": 0,
            "impact": HIGH,
            "avoid_before_min": 60, "avoid_after_min": 30,
            "currencies": ["GBP"],
            "description": "BOE Decision des Taux",
            "historical_impact": 4,
        },
    }

    def __init__(self, config: dict):
        self.config = config
        nf = config.get("news_filter", {})
        self.enabled = nf.get("enabled", True)
        self.avoid_high_impact = nf.get("avoid_high_impact", True)
        self.avoid_medium_impact = nf.get("avoid_medium_impact", True)
        self.avoid_low_impact = nf.get("avoid_low_impact", False)
        self.custom_avoid_minutes = nf.get("custom_avoid_minutes", None)
        self.custom_avoid_before_minutes = nf.get("custom_avoid_before_minutes", None)
        self.custom_avoid_after_minutes = nf.get("custom_avoid_after_minutes", None)
        self.mode = nf.get("mode", "block")  # block / warn / reduce
        self.cache_file = nf.get("cache_file", "news_cache.json")
        self.web_fetch = nf.get("web_fetch", True)
        self.cache_hours = nf.get("cache_hours", 4)
        self.upcoming_events: List[Dict] = []
        self.web_events: List[Dict] = []
        self._last_fetch = None
        self._sentiment_data: Dict[str, str] = {}  # event_name -> sentiment
        self._load_cache()

        # Whitelist: symboles NON affectes par le filtre
        self.whitelist: Set[str] = set(nf.get("whitelist", []))

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                self.web_events = data.get("web_events", [])
                self.upcoming_events = data.get("events", [])
                self._sentiment_data = data.get("sentiment_data", {})
                last = data.get("last_web_fetch", "")
                if last:
                    self._last_fetch = datetime.fromisoformat(last)
            except Exception:
                pass

    def _save_cache(self):
        try:
            with open(self.cache_file, "w") as f:
                json.dump({
                    "events": self.upcoming_events,
                    "web_events": self.web_events,
                    "last_web_fetch": self._last_fetch.isoformat() if self._last_fetch else "",
                    "sentiment_data": self._sentiment_data,
                }, f, indent=2)
        except Exception:
            pass

    def is_safe_to_trade(self, symbol: str = "") -> Tuple[bool, str]:
        """
        Verifie s'il est sur de trader maintenant pour un symbole donne.

        Returns:
            (safe, reason)
            safe=True -> on peut trader
            safe=False -> une news approche et affecte ce symbole
        """
        if not self.enabled:
            return True, "Filtre news desactive"

        if symbol and symbol in self.whitelist:
            return True, f"{symbol} en whitelist (exempte du filtre)"

        now = datetime.now(timezone.utc)

        # Essayer de fetch depuis le web si necessaire (rafraichissement adaptatif)
        if self.web_fetch and self._should_refresh(now):
            self._fetch_web_news()

        # Combiner evenements web + internes
        all_events = self._get_upcoming_events(now)
        self.upcoming_events = all_events
        self._save_cache()

        for event in all_events:
            if not self._should_avoid(event):
                continue

            # Verifier si cette news affecte le symbole
            if symbol and not self._affects_symbol(event, symbol):
                continue

            minutes_to_event = (event["datetime"] - now).total_seconds() / 60

            # Zones d'evitement pre/post-news configurables separement
            before_min = self.custom_avoid_before_minutes or self.custom_avoid_minutes or event.get("avoid_before_min", event.get("avoid_minutes", 30))
            after_min = self.custom_avoid_after_minutes or event.get("avoid_after_min", event.get("avoid_minutes", 30))

            # Zone d'evitement asymetrique
            if -after_min <= minutes_to_event <= before_min:
                if minutes_to_event > 0:
                    direction = "dans"
                    time_left = minutes_to_event
                    zone = "PRE-NEWS"
                else:
                    direction = "depuis"
                    time_left = abs(minutes_to_event)
                    zone = "POST-NEWS"

                # Score d'impact historique
                hist_impact = event.get("historical_impact", event.get("impact", 3))
                impact_label = self.IMPACT_LABELS.get(hist_impact, "INCONNU")

                # Analyse de sentiment
                sentiment_info = self._get_sentiment_str(event)

                reason = (
                    f"NEWS [{impact_label}] {event['name']} "
                    f"{direction} {time_left:.0f} min ({zone}) "
                    f"(eviter {before_min}min avant / {after_min}min apres) "
                    f"- {event['description']}"
                    f"{sentiment_info}"
                )

                # Force devise pendant l'evenement
                currency_str = self._get_currency_strength_str(event)
                if currency_str:
                    reason += f" | {currency_str}"

                if self.mode == "warn":
                    logger.warning(f"[FILTRE NEWS - AVERTISSEMENT] {reason}")
                    return True, reason
                elif self.mode == "reduce":
                    logger.warning(f"[FILTRE NEWS - REDUCTION] {reason}")
                    return True, f"REDUCE:{reason}"
                else:
                    logger.warning(f"[FILTRE NEWS - BLOCAGE] {reason}")
                    return False, reason

        return True, "Pas de news a proximite"

    def get_lot_multiplier(self, symbol: str = "") -> float:
        """
        Retourne un multiplicateur de mise base sur la proximite des news.
        1.0 = normal, 0.5 = moitie, 0.25 = quart.
        Prend en compte le score d'impact historique.
        """
        if not self.enabled:
            return 1.0
        safe, reason = self.is_safe_to_trade(symbol)
        if safe and not reason.startswith("REDUCE:"):
            return 1.0
        # Si on est en mode reduce, calculer le multiplicateur
        now = datetime.now(timezone.utc)
        min_mult = 1.0
        for event in self.upcoming_events:
            if not self._affects_symbol(event, symbol):
                continue
            minutes_to = (event["datetime"] - now).total_seconds() / 60
            before_min = self.custom_avoid_before_minutes or self.custom_avoid_minutes or event.get("avoid_before_min", 30)
            if 0 < minutes_to < before_min:
                ratio = minutes_to / before_min
                # Score d'impact historique influence la reduction
                hist_impact = event.get("historical_impact", event.get("impact", 3))
                impact_factor = hist_impact / 5.0  # 0.2 a 1.0

                if ratio > 0.66:
                    mult = 0.75
                elif ratio > 0.33:
                    mult = 0.5
                else:
                    mult = 0.25

                # Reduire davantage si impact historique eleve
                mult *= (1.0 - impact_factor * 0.3)
                mult = max(0.15, mult)
                min_mult = min(min_mult, mult)
        return round(min_mult, 2)

    def get_upcoming_impact(self, hours: float = 2.0, symbol: str = "") -> float:
        """
        Retourne le score d'impact agregé pour les N prochaines heures.

        Args:
            hours: Nombre d'heures a regarder en avant
            symbol: Symbole filtre (vide = tous)

        Returns:
            Score d'impact de 0.0 (aucun evenement) a 10.0+ (evenements majeurs).
            L'impact decroit avec le temps restant avant l'evenement.
        """
        now = datetime.now(timezone.utc)
        window = timedelta(hours=hours)
        total_impact = 0.0

        for event in self.upcoming_events:
            if event["datetime"] < now:
                continue
            if event["datetime"] > now + window:
                continue
            if symbol and not self._affects_symbol(event, symbol):
                continue

            # Impact de base
            hist_impact = event.get("historical_impact", event.get("impact", 3))

            # Facteur de proximite: plus c'est proche, plus l'impact est fort
            minutes_to = (event["datetime"] - now).total_seconds() / 60
            before_min = event.get("avoid_before_min", 30)
            proximity = max(0.0, 1.0 - (minutes_to / (before_min * 2)))

            # Sentiment amplifie l'impact
            sentiment_mult = 1.0
            sentiment = event.get("sentiment", "")
            if sentiment in ("meilleur", "pire"):
                sentiment_mult = 1.3
            elif sentiment in ("beaucoup_mieux", "beaucoup_pire"):
                sentiment_mult = 1.5

            total_impact += hist_impact * proximity * sentiment_mult

        return round(total_impact, 2)

    def _should_avoid(self, event: Dict) -> bool:
        """Verifie si l'evenement doit etre evite selon son impact."""
        impact = event["impact"]
        hist_impact = event.get("historical_impact", impact)
        effective_impact = max(impact, hist_impact)

        if effective_impact >= self.HIGH and self.avoid_high_impact:
            return True
        if effective_impact >= self.MEDIUM and self.avoid_medium_impact:
            return True
        if effective_impact >= self.LOW and self.avoid_low_impact:
            return True
        return False

    def _affects_symbol(self, event: Dict, symbol: str) -> bool:
        """Verifie si un evenement de news affecte un symbole."""
        currencies = event.get("currencies", ["USD"])
        affected_symbols: Set[str] = set()
        for currency in currencies:
            affected_symbols.update(CURRENCY_SYMBOL_MAP.get(currency, []))

        if symbol in ALWAYS_SENSITIVE and any(c in ["USD", "GLOBAL"] for c in currencies):
            return True

        return symbol in affected_symbols

    def _should_refresh(self, now: Optional[datetime] = None) -> bool:
        """
        Rafraichissement adaptatif: plus frequent quand une news approche.
        Normal: toutes les cache_hours
        Proximite d'une news: toutes les 15 minutes
        """
        if self._last_fetch is None:
            return True

        if now is None:
            now = datetime.now(timezone.utc)

        elapsed = (now - self._last_fetch).total_seconds() / 60  # en minutes

        # Verifier si une news approche dans les 2h
        min_minutes_to_news = float("inf")
        for event in self.upcoming_events:
            if event["datetime"] > now:
                mins = (event["datetime"] - now).total_seconds() / 60
                hist_impact = event.get("historical_impact", event.get("impact", 1))
                if hist_impact >= 3:  # Seulement pour les news a impact moyen+
                    min_minutes_to_news = min(min_minutes_to_news, mins)

        # Rafraichir plus souvent si une news importante approche
        if min_minutes_to_news < 120:
            return elapsed >= 15  # Toutes les 15 minutes
        elif min_minutes_to_news < 360:
            return elapsed >= 60  # Toutes les heures

        # Normal: toutes les cache_hours
        return elapsed >= (self.cache_hours * 60)

    def _fetch_web_news(self):
        """Tente de recuperer les news depuis plusieurs sources web."""
        for source in WEB_NEWS_SOURCES:
            if source.get("fallback_only") and len(self.web_events) > 0:
                continue
            try:
                url = source["url"]
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")

                if source["type"] == "rss":
                    self._parse_rss(content)
                elif source["type"] == "html":
                    self._parse_html_calendar(content)

                self._last_fetch = datetime.now(timezone.utc)
                logger.info(
                    f"[News] Source '{source['name']}': "
                    f"{len(self.web_events)} evenements charges"
                )
                break  # Succes, pas besoin d'essayer les autres

            except Exception as e:
                logger.debug(
                    f"[News] Impossible de fetch depuis '{source['name']}': {e}"
                )
                continue

    def _parse_rss(self, content: str):
        """Parse le RSS de ForexFactory."""
        events = []
        items = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)

        for item in items:
            title_match = re.search(
                r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.DOTALL
            )
            date_match = re.search(r"<pubDate>(.*?)</pubDate>", item)

            if not title_match or not date_match:
                continue

            title = title_match.group(1).strip()
            date_str = date_match.group(1).strip()

            try:
                dt = self._parse_rss_date(date_str)
            except Exception:
                continue

            impact = self._guess_impact(title)
            currencies = self._guess_currencies(title)
            hist_impact = self._estimate_historical_impact(title, impact)
            sentiment = self._detect_sentiment(title)

            if impact > 0:
                events.append({
                    "name": title[:50],
                    "datetime": dt,
                    "impact": impact,
                    "historical_impact": hist_impact,
                    "impact_str": self.IMPACT_LABELS.get(hist_impact, "INCONNU"),
                    "avoid_before_min": self._compute_avoid_before(impact, hist_impact),
                    "avoid_after_min": self._compute_avoid_after(impact, hist_impact),
                    "currencies": currencies,
                    "description": title,
                    "source": "web",
                    "sentiment": sentiment,
                })

        self.web_events = events

    def _parse_html_calendar(self, content: str):
        """Parse un calendrier HTML basique (fallback)."""
        # Extraction basique d'evenements depuis du HTML
        # Recherche de patterns de date + evenement
        date_patterns = re.findall(
            r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})',
            content, re.IGNORECASE
        )
        if date_patterns:
            logger.debug(
                f"[News] Calendrier HTML: {len(date_patterns)} dates detectees"
            )

    def _parse_rss_date(self, date_str: str) -> datetime:
        """Parse une date RSS en datetime UTC."""
        for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S"]:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
        raise ValueError(f"Impossible de parser: {date_str}")

    def _guess_impact(self, title: str) -> int:
        """Devine l'impact d'une news depuis son titre."""
        extreme_words = ["NFP", "Non-Farm", "FOMC Decision", "Rate Decision"]
        high_words = ["FOMC", "CPI", "Employment", "GDP", "Fed",
                       "ECB", "BOE", "Banque Centrale"]
        medium_words = ["PMI", "Retail", "Unemployment", "Trade Balance",
                         "ADP", "PPI", "ISM", "Confidence", "Housing",
                         "Chomage", "PIB", "Ventes"]
        title_upper = title.upper()
        if any(w.upper() in title_upper for w in extreme_words):
            return self.HIGH
        if any(w.upper() in title_upper for w in high_words):
            return self.HIGH
        if any(w.upper() in title_upper for w in medium_words):
            return self.MEDIUM
        return self.LOW

    def _estimate_historical_impact(self, title: str, base_impact: int) -> int:
        """
        Estime l'impact historique (1-5) d'une news.
        Base sur le type d'evenement et son impact historique de volatilite.
        """
        title_upper = title.upper()

        # Evenements a impact extreme (5)
        extreme_patterns = ["NFP", "NON-FARM", "FOMC", "EMPLOYMENT SITUATION"]
        if any(p in title_upper for p in extreme_patterns):
            return 5

        # Evenements a tres fort impact (4)
        very_high_patterns = ["RATE DECISION", "CPI", "FED", "ECB", "BOE",
                               "BANQUE CENTRALE", "GDP", "PIB", "FOMC MINUTES"]
        if any(p in title_upper for p in very_high_patterns):
            return 4

        # Evenements a fort impact (3)
        high_patterns = ["PPI", "RETAIL", "UNEMPLOYMENT", "PMI",
                          "HOUSING", "CONFIDENCE", "CONSUMER"]
        if any(p in title_upper for p in high_patterns):
            return 3

        # Par defaut, utiliser l'impact de base
        return min(5, base_impact + 1)

    def _compute_avoid_before(self, impact: int, hist_impact: int) -> int:
        """Calcule les minutes d'evitement avant la news selon l'impact."""
        if hist_impact >= 5:
            return 120
        elif hist_impact >= 4:
            return 90
        elif hist_impact >= 3:
            return 45
        elif hist_impact >= 2:
            return 20
        return 10

    def _compute_avoid_after(self, impact: int, hist_impact: int) -> int:
        """Calcule les minutes d'evitement apres la news selon l'impact."""
        if hist_impact >= 5:
            return 60
        elif hist_impact >= 4:
            return 45
        elif hist_impact >= 3:
            return 20
        elif hist_impact >= 2:
            return 10
        return 5

    def _detect_sentiment(self, title: str) -> str:
        """
        Detecte le sentiment d'une news depuis son titre.
        Recherche des mots-cles indiquant si c'est meilleur/pire que prevu.
        """
        title_lower = title.lower()

        if any(w in title_lower for w in ["beat", "above", "better", "exceeds"]):
            return "meilleur"
        if any(w in title_lower for w in ["miss", "below", "worse", "falls short"]):
            return "pire"
        if any(w in title_lower for w in ["well above", "smashed", "surged"]):
            return "beaucoup_mieux"
        if any(w in title_lower for w in ["well below", "crashed", "plunged"]):
            return "beaucoup_pire"
        return ""

    def _get_sentiment_str(self, event: Dict) -> str:
        """Retourne une chaine descriptive du sentiment."""
        sentiment = event.get("sentiment", "")
        if sentiment == "meilleur":
            return " [Sentiment: MEILLEUR que prevu]"
        elif sentiment == "pire":
            return " [Sentiment: PIRE que prevu]"
        elif sentiment == "beaucoup_mieux":
            return " [Sentiment: BEAUCOUP MEILLEUR que prevu - IMPACT ELEVE]"
        elif sentiment == "beaucoup_pire":
            return " [Sentiment: BEAUCOUP PIRE que prevu - IMPACT ELEVE]"
        return ""

    def _get_currency_strength_str(self, event: Dict) -> str:
        """
        Analyse la force des devises pendant cet evenement.
        Retourne une chaine descriptive.
        """
        currencies = event.get("currencies", [])
        event_name = event.get("name", "")
        strengths = CURRENCY_STRENGTH_BY_EVENT.get(event_name, {})

        if not strengths:
            return ""

        parts = []
        for currency, strength in strengths.items():
            if currency in currencies:
                label = self.IMPACT_LABELS.get(int(min(5, strength)), "")
                parts.append(f"{currency}={label}")

        if parts:
            return f"Force devise: {', '.join(parts)}"
        return ""

    def _guess_currencies(self, title: str) -> List[str]:
        """Devine les devises concernees depuis le titre."""
        currencies = []
        title_upper = title.upper()
        for c in ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"]:
            if c in title_upper:
                currencies.append(c)
        if "GOLD" in title_upper or "XAU" in title_upper:
            currencies.append("XAU")
        if not currencies:
            currencies.append("USD")
        if any(w in title_upper for w in ["GLOBAL", "INDEX", "MARKET"]):
            currencies.append("GLOBAL")
        return currencies

    def _get_upcoming_events(self, now: datetime) -> List[Dict]:
        """Combine evenements web + calendrier interne (prochaines 48h)."""
        events = list(self.web_events)
        window = timedelta(hours=48)

        for name, info in self.RECURRING_EVENTS.items():
            ev_type = info["type"]
            dates = []

            if ev_type == "first_friday":
                for m_offset in range(3):
                    d = self._first_friday(now.year, now.month + m_offset)
                    dt = datetime(d[0], d[1], d[2],
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "monthly":
                for m_offset in range(3):
                    year = now.year
                    month = now.month + m_offset
                    year, month = self._normalize_ym(year, month)
                    day = min(info["day_offset"], self._days_in_month(year, month))
                    dt = datetime(year, month, day,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "quarterly":
                months = info["months"]
                for m in months:
                    for y_offset in range(2):
                        year = now.year + y_offset
                        month = m
                        day = min(info["day_offset"], self._days_in_month(year, month))
                        dt = datetime(year, month, day,
                                       info["hour_utc"], info["minute_utc"],
                                       tzinfo=timezone.utc)
                        dates.append(dt)

            elif ev_type == "fomc_dates":
                for (y, m, d) in info["dates"]:
                    dt = datetime(y, m, d,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "fomc_minutes":
                for (y, m, d) in info["dates"]:
                    dt = datetime(y, m, d,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "ecb_dates":
                for (y, m, d) in info["dates"]:
                    dt = datetime(y, m, d,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "boe_dates":
                for (y, m, d) in info["dates"]:
                    dt = datetime(y, m, d,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "weekly_thursday":
                for w_offset in range(8):
                    d = now + timedelta(days=w_offset)
                    while d.weekday() != 3:
                        d += timedelta(days=1)
                    dt = datetime(d.year, d.month, d.day,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            elif ev_type == "weekly_varies":
                # Discours FED: generer 2 evenements par semaine
                for w_offset in range(4):
                    base = now + timedelta(days=w_offset * 3 + 1)
                    dt = datetime(base.year, base.month, base.day,
                                   info["hour_utc"], info["minute_utc"],
                                   tzinfo=timezone.utc)
                    dates.append(dt)

            for dt in dates:
                if dt - window <= now <= dt + window:
                    avoid_before = (self.custom_avoid_before_minutes
                                    or self.custom_avoid_minutes
                                    or info.get("avoid_before_min", info.get("avoid_minutes", 30)))
                    avoid_after = (self.custom_avoid_after_minutes
                                   or info.get("avoid_after_min", info.get("avoid_minutes", 30)))
                    impact = info["impact"]
                    hist_impact = info.get("historical_impact", impact)
                    events.append({
                        "name": name,
                        "datetime": dt,
                        "impact": impact,
                        "historical_impact": hist_impact,
                        "impact_str": self.IMPACT_LABELS.get(hist_impact, "INCONNU"),
                        "avoid_before_min": avoid_before,
                        "avoid_after_min": avoid_after,
                        "currencies": info.get("currencies", ["USD"]),
                        "avoid_minutes": max(avoid_before, avoid_after),
                        "description": info["description"],
                        "source": "internal",
                        "sentiment": "",
                    })

        events.sort(key=lambda x: x["datetime"])
        return events

    def _first_friday(self, year: int, month: int) -> Tuple[int, int, int]:
        year, month = self._normalize_ym(year, month)
        first_day = datetime(year, month, 1)
        days_until_friday = (4 - first_day.weekday()) % 7
        day = 1 + days_until_friday
        return (year, month, day)

    @staticmethod
    def _normalize_ym(year: int, month: int) -> Tuple[int, int]:
        while month > 12:
            year += 1
            month -= 12
        while month < 1:
            year -= 1
            month += 12
        return year, month

    @staticmethod
    def _days_in_month(year: int, month: int) -> int:
        return (datetime(year, month + 1, 1) - timedelta(days=1)).day if month < 12 else 31

    def get_next_events(self, count: int = 5) -> List[Dict]:
        now = datetime.now(timezone.utc)
        upcoming = [e for e in self.upcoming_events if e["datetime"] > now]
        return upcoming[:count]

    def status_report(self) -> str:
        if not self.enabled:
            return "Filtre news desactive"
        events = self.get_next_events(5)
        web_count = len(self.web_events)
        lines = [
            f"Filtre News v3: ACTIVE (mode={self.mode})",
            f"  News web: {web_count} | Prochaines 48h: {len(self.upcoming_events)}",
            f"  Zones pre-news: configurable separement",
            f"  Zones post-news: configurable separement",
            f"  Rafraichissement adaptatif: actif",
        ]
        if events:
            lines.append("  Prochaines news:")
            for ev in events:
                dt = ev["datetime"].strftime("%d/%m %H:%M UTC")
                src = "[WEB]" if ev.get("source") == "web" else "[CAL]"
                cur = ",".join(ev.get("currencies", []))
                hist = ev.get("historical_impact", ev.get("impact", 0))
                impact_lbl = self.IMPACT_LABELS.get(hist, "?")
                sentiment = ev.get("sentiment", "")
                sent_str = f" ({sentiment})" if sentiment else ""
                lines.append(
                    f"    {src} [{impact_lbl}] {ev['name']} ({dt}) {cur}{sent_str}"
                )
        else:
            lines.append("  Aucune news importante a venir (48h)")
        return "\n".join(lines)
