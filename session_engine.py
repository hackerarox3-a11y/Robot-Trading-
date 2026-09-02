"""UTC market session engine with strategy bias and alert scheduling."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


class SessionEngine:
    SESSIONS = {"sydney": (21, 6), "tokyo": (0, 9), "london": (7, 16), "new_york": (12, 21)}
    PREFERENCES = {"sydney": "conservative", "tokyo": "range_and_liquidity", "london": "breakout_and_trend", "new_york": "momentum_and_breakout"}

    def __init__(self, config: Optional[dict] = None):
        self.alert_minutes = tuple((config or {}).get("session_engine", {}).get("alert_minutes", (30, 10)))
        self._sent_alerts = set()

    @staticmethod
    def _in_window(hour: float, start: int, end: int) -> bool:
        return hour >= start or hour < end if start > end else start <= hour < end

    def active_sessions(self, now: Optional[datetime] = None) -> List[str]:
        now = now or datetime.now(timezone.utc)
        return [name for name, (start, end) in self.SESSIONS.items() if self._in_window(now.hour + now.minute / 60, start, end)]

    def analyze(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        active = self.active_sessions(now)
        primary = "new_york" if "new_york" in active else "london" if "london" in active else active[0] if active else "closed"
        return {"active_sessions": active, "primary_session": primary, "strategy_mode": self.PREFERENCES.get(primary, "no_trade"), "is_overlap": len(active) > 1, "utc_time": now.isoformat()}

    def upcoming_alerts(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        alerts = []
        for name, (start, _end) in self.SESSIONS.items():
            target = now.replace(hour=start, minute=0, second=0, microsecond=0)
            if target <= now: target += timedelta(days=1)
            minutes = (target - now).total_seconds() / 60
            for lead in self.alert_minutes:
                key = (name, lead, target.date())
                if 0 <= minutes <= lead and key not in self._sent_alerts:
                    self._sent_alerts.add(key)
                    alerts.append({"session": name, "minutes_before": lead, "starts_at": target.isoformat(), "message": "%s session dans %d minutes" % (name.title(), lead)})
        return alerts

    def clear_alerts(self) -> None:
        self._sent_alerts.clear()


MarketSessionEngine = SessionEngine
