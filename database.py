"""SQLite persistence and performance statistics for the trading bot."""

import math
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class Database:
    """Thread-safe SQLite database with automatic schema creation and commits."""

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = Path(db_path)
        if self.db_path.parent != Path("."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    broker TEXT, symbol TEXT, direction TEXT,
                    entry REAL, exit REAL, sl REAL, tp REAL,
                    profit REAL NOT NULL DEFAULT 0,
                    duration REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    quality REAL NOT NULL DEFAULT 0,
                    reason TEXT, metadata TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    broker TEXT, symbol TEXT, signal TEXT,
                    confidence REAL NOT NULL DEFAULT 0,
                    quality REAL NOT NULL DEFAULT 0,
                    metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS market_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    broker TEXT, symbol TEXT,
                    score REAL NOT NULL DEFAULT 0,
                    recommendation TEXT, metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS risk_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    broker TEXT, event TEXT,
                    risk_percent REAL, drawdown_percent REAL,
                    equity REAL, balance REAL, details TEXT
                );
                CREATE TABLE IF NOT EXISTS performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    period TEXT, balance REAL, equity REAL,
                    pnl REAL, win_rate REAL, profit_factor REAL,
                    sharpe_ratio REAL, drawdown REAL, metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS ai_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    broker TEXT, symbol TEXT,
                    buy_probability REAL, sell_probability REAL,
                    confidence REAL, expected_rr REAL,
                    prediction TEXT, metadata TEXT
                );
                """
            )
            self.connection.commit()

    @staticmethod
    def _timestamp(value: Optional[Any] = None) -> str:
        if value is None:
            return datetime.utcnow().isoformat(timespec="seconds") + "Z"
        return str(value)

    @staticmethod
    def _metadata(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        import json
        return json.dumps(value, default=str, ensure_ascii=True)

    def _insert(self, table: str, values: Dict[str, Any]) -> int:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._lock:
            cursor = self.connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def record_trade(self, trade: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        """Persist a trade, including entry/exit, SL/TP and quality fields."""
        data = dict(trade or {})
        data.update(kwargs)
        return self._insert("trades", {
            "timestamp": self._timestamp(data.pop("timestamp", None)),
            "broker": data.get("broker"), "symbol": data.get("symbol"),
            "direction": data.get("direction"), "entry": data.get("entry", data.get("entry_price")),
            "exit": data.get("exit", data.get("exit_price")), "sl": data.get("sl"), "tp": data.get("tp"),
            "profit": float(data.get("profit", data.get("pnl", 0)) or 0),
            "duration": float(data.get("duration", 0) or 0),
            "confidence": float(data.get("confidence", 0) or 0),
            "quality": float(data.get("quality", data.get("quality_score", 0)) or 0),
            "reason": data.get("reason"), "metadata": self._metadata(data.get("metadata")),
        })

    def record_signal(self, signal: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        data = dict(signal or {})
        data.update(kwargs)
        return self._insert("signals", {
            "timestamp": self._timestamp(data.pop("timestamp", None)),
            "broker": data.get("broker"), "symbol": data.get("symbol"),
            "signal": data.get("signal", data.get("direction")),
            "confidence": float(data.get("confidence", 0) or 0),
            "quality": float(data.get("quality", data.get("quality_score", 0)) or 0),
            "metadata": self._metadata(data.get("metadata")),
        })

    def record_market_score(self, score: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        data = dict(score or {})
        data.update(kwargs)
        return self._insert("market_scores", {
            "timestamp": self._timestamp(data.pop("timestamp", None)),
            "broker": data.get("broker"), "symbol": data.get("symbol"),
            "score": float(data.get("score", 0) or 0),
            "recommendation": data.get("recommendation"),
            "metadata": self._metadata(data.get("metadata")),
        })

    def record_risk_history(self, risk: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        data = dict(risk or {})
        data.update(kwargs)
        return self._insert("risk_history", {
            "timestamp": self._timestamp(data.pop("timestamp", None)),
            "broker": data.get("broker"), "event": data.get("event"),
            "risk_percent": data.get("risk_percent", data.get("risk_pct")),
            "drawdown_percent": data.get("drawdown_percent", data.get("drawdown_pct")),
            "equity": data.get("equity"), "balance": data.get("balance"),
            "details": self._metadata(data.get("details", data.get("metadata"))),
        })

    def record_performance(self, performance: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        data = dict(performance or {})
        data.update(kwargs)
        return self._insert("performance", {
            "timestamp": self._timestamp(data.pop("timestamp", None)),
            "period": data.get("period"), "balance": data.get("balance"),
            "equity": data.get("equity"), "pnl": data.get("pnl"),
            "win_rate": data.get("win_rate"), "profit_factor": data.get("profit_factor"),
            "sharpe_ratio": data.get("sharpe_ratio"), "drawdown": data.get("drawdown"),
            "metadata": self._metadata(data.get("metadata")),
        })

    def record_ai_prediction(self, prediction: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        data = dict(prediction or {})
        data.update(kwargs)
        return self._insert("ai_predictions", {
            "timestamp": self._timestamp(data.pop("timestamp", None)),
            "broker": data.get("broker"), "symbol": data.get("symbol"),
            "buy_probability": data.get("buy_probability"),
            "sell_probability": data.get("sell_probability"),
            "confidence": data.get("confidence"), "expected_rr": data.get("expected_rr"),
            "prediction": data.get("prediction", data.get("signal")),
            "metadata": self._metadata(data.get("metadata")),
        })

    def _profits(self, since: Optional[str] = None) -> list:
        query = "SELECT profit FROM trades"
        params = ()
        if since:
            query += " WHERE timestamp >= ?"
            params = (since,)
        query += " ORDER BY timestamp, id"
        with self._lock:
            return [float(row[0]) for row in self.connection.execute(query, params)]

    def win_rate(self, since: Optional[str] = None) -> float:
        profits = self._profits(since)
        return sum(profit > 0 for profit in profits) / len(profits) if profits else 0.0

    def profit_factor(self, since: Optional[str] = None) -> float:
        profits = self._profits(since)
        gains = sum(profit for profit in profits if profit > 0)
        losses = abs(sum(profit for profit in profits if profit < 0))
        if losses == 0:
            return math.inf if gains > 0 else 0.0
        return gains / losses

    def sharpe_ratio(self, since: Optional[str] = None) -> float:
        profits = self._profits(since)
        if len(profits) < 2:
            return 0.0
        mean = sum(profits) / len(profits)
        variance = sum((profit - mean) ** 2 for profit in profits) / (len(profits) - 1)
        deviation = math.sqrt(variance)
        return mean / deviation * math.sqrt(len(profits)) if deviation else 0.0

    def drawdown(self, initial_balance: float = 0.0, since: Optional[str] = None) -> Dict[str, float]:
        equity = float(initial_balance)
        peak = equity
        maximum_amount = 0.0
        for profit in self._profits(since):
            equity += profit
            peak = max(peak, equity)
            maximum_amount = max(maximum_amount, peak - equity)
        maximum_percent = maximum_amount / peak * 100 if peak > 0 else 0.0
        return {"amount": maximum_amount, "percent": maximum_percent, "peak": peak, "equity": equity}

    def get_statistics(self, initial_balance: float = 0.0, since: Optional[str] = None) -> Dict[str, float]:
        profits = self._profits(since)
        drawdown = self.drawdown(initial_balance, since)
        return {
            "trades": len(profits), "win_rate": self.win_rate(since),
            "profit_factor": self.profit_factor(since),
            "sharpe_ratio": self.sharpe_ratio(since),
            "drawdown": drawdown["percent"], "drawdown_amount": drawdown["amount"],
            "total_profit": sum(profits),
        }

    def close(self) -> None:
        with self._lock:
            self.connection.close()


DatabaseEngine = Database
_default_database = Database()


def get_statistics(initial_balance: float = 0.0, since: Optional[str] = None) -> Dict[str, float]:
    return _default_database.get_statistics(initial_balance, since)


def win_rate(since: Optional[str] = None) -> float:
    return _default_database.win_rate(since)


def profit_factor(since: Optional[str] = None) -> float:
    return _default_database.profit_factor(since)


def sharpe_ratio(since: Optional[str] = None) -> float:
    return _default_database.sharpe_ratio(since)


def drawdown(initial_balance: float = 0.0, since: Optional[str] = None) -> Dict[str, float]:
    return _default_database.drawdown(initial_balance, since)
