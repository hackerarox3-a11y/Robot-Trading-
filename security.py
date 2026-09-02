"""Environment validation and safe trading-mode controls."""
import os
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

VALID_MODES = ("PAPER", "SIMULATION", "LIVE")


def load_environment(dotenv_path: Optional[str] = None) -> bool:
    if load_dotenv is None:
        return False
    return bool(load_dotenv(dotenv_path, override=False) if dotenv_path else load_dotenv(override=False))


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def validate_environment(mode: str = "SIMULATION", active_broker: str = "mt5",
                         telegram_enabled: bool = False) -> Dict[str, object]:
    """Validate required environment variables without ever printing secrets."""
    normalized_mode = str(mode or "SIMULATION").upper()
    missing: List[str] = []
    warnings: List[str] = []
    if normalized_mode not in VALID_MODES:
        return {"valid": False, "mode": normalized_mode, "missing": [], "warnings": ["Mode invalide: PAPER, SIMULATION ou LIVE requis"]}
    if normalized_mode == "LIVE":
        if active_broker in ("mt5", "both"):
            for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
                if not _value(name): missing.append(name)
        if active_broker in ("deriv", "both"):
            if not _value("DERIV_APP_ID"): missing.append("DERIV_APP_ID")
            if not (_value("DERIV_ACCESS_TOKEN") or _value("DERIV_OAUTH_TOKEN") or _value("DERIV_API_TOKEN")):
                missing.append("DERIV_ACCESS_TOKEN")
    if telegram_enabled:
        if not (_value("TELEGRAM_TOKEN") or _value("TELEGRAM_BOT_TOKEN")): missing.append("TELEGRAM_TOKEN")
        if not _value("TELEGRAM_CHAT_ID"): missing.append("TELEGRAM_CHAT_ID")
    if normalized_mode != "LIVE":
        warnings.append("Aucun ordre réel autorisé en mode %s" % normalized_mode)
    return {"valid": not missing, "mode": normalized_mode, "missing": missing, "warnings": warnings}


def require_live_environment(mode: str, active_broker: str, telegram_enabled: bool = False) -> None:
    result = validate_environment(mode, active_broker, telegram_enabled)
    if not result["valid"]:
        raise RuntimeError("Configuration LIVE incomplete. Variables manquantes: %s" % ", ".join(result["missing"]))


def is_live(mode: str) -> bool:
    return str(mode).upper() == "LIVE"
