"""Lightweight RandomForest predictor for trading signals."""

import csv
import os
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class AIPredictor:
    """Train and persist a small classifier using trading indicators."""

    FEATURE_NAMES = (
        "ema", "rsi", "macd", "atr", "volume", "adx",
        "hour", "day", "trade_history",
    )
    LABELS = ("BUY", "SELL")

    def __init__(self, model_path: str = "ai_predictor_model.pkl", random_state: int = 42):
        self.model_path = Path(model_path)
        self.random_state = random_state
        self.model = None
        self.expected_rr = 1.0
        self.training_metrics: Dict[str, Any] = {}
        self._load_model()

    def _load_model(self) -> bool:
        if not self.model_path.exists():
            return False
        try:
            with self.model_path.open("rb") as model_file:
                saved = pickle.load(model_file)
            self.model = saved.get("model") if isinstance(saved, dict) else saved
            if isinstance(saved, dict):
                self.expected_rr = float(saved.get("expected_rr", 1.0))
                self.training_metrics = saved.get("metrics", {})
            return self.model is not None
        except (OSError, pickle.PickleError, EOFError, ValueError, TypeError):
            self.model = None
            return False

    @classmethod
    def _feature_row(cls, record: Dict[str, Any]) -> List[float]:
        aliases = {
            "ema": ("ema", "ema_signal", "ema_fast", "ema_trend"),
            "rsi": ("rsi",),
            "macd": ("macd", "macd_line", "macd_hist"),
            "atr": ("atr",),
            "volume": ("volume", "tick_volume"),
            "adx": ("adx",),
            "hour": ("hour",),
            "day": ("day", "weekday"),
            "trade_history": ("trade_history", "history", "recent_win_rate"),
        }
        values = []
        for feature in cls.FEATURE_NAMES:
            value = next((record.get(key) for key in aliases[feature] if record.get(key) is not None), 0.0)
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                values.append(0.0)
        return values

    @staticmethod
    def _records(source: Any) -> List[Dict[str, Any]]:
        if source is None:
            return []
        if isinstance(source, (str, os.PathLike, Path)):
            with Path(source).open("r", encoding="utf-8", newline="") as data_file:
                return list(csv.DictReader(data_file))
        if hasattr(source, "to_dict"):
            return list(source.to_dict("records"))
        return [dict(record) for record in source]

    @staticmethod
    def _label(record: Dict[str, Any]) -> Optional[str]:
        explicit = str(record.get("label", record.get("signal", ""))).upper()
        if explicit in ("BUY", "SELL"):
            return explicit
        direction = str(record.get("direction", "")).upper()
        if direction not in ("BUY", "SELL"):
            return None
        try:
            pnl = float(record.get("pnl", 0.0) or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        if pnl == 0:
            return direction
        return direction if pnl > 0 else ("SELL" if direction == "BUY" else "BUY")

    def train_model(
        self,
        history: Any = None,
        model_path: Optional[str] = None,
        n_estimators: int = 100,
    ) -> Dict[str, Any]:
        """Train a RandomForest and save it automatically."""
        if model_path is not None:
            self.model_path = Path(model_path)
        if history is None:
            history = "trades_history.csv"
        records = self._records(history)
        rows: List[List[float]] = []
        labels: List[str] = []
        rr_values: List[float] = []
        for record in records:
            label = self._label(record)
            if label is None:
                continue
            row = self._feature_row(record)
            rows.append(row)
            labels.append(label)
            try:
                rr = float(record.get("expected_rr", record.get("rr", 0.0)) or 0.0)
                if rr > 0:
                    rr_values.append(rr)
            except (TypeError, ValueError):
                pass
        if len(rows) < 2:
            raise ValueError("Au moins deux trades etiquetes sont necessaires")
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError as error:
            raise RuntimeError("scikit-learn est requis pour entrainer le modele") from error

        self.model = RandomForestClassifier(
            n_estimators=max(10, int(n_estimators)),
            max_depth=6,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=self.random_state,
        )
        self.model.fit(rows, labels)
        self.expected_rr = sum(rr_values) / len(rr_values) if rr_values else 1.0
        self.training_metrics = {
            "samples": len(rows),
            "buy_samples": labels.count("BUY"),
            "sell_samples": labels.count("SELL"),
            "features": list(self.FEATURE_NAMES),
            "trained_at": datetime.utcnow().isoformat() + "Z",
        }
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        with self.model_path.open("wb") as model_file:
            pickle.dump({
                "model": self.model,
                "expected_rr": self.expected_rr,
                "metrics": self.training_metrics,
            }, model_file)
        return dict(self.training_metrics, model_path=str(self.model_path))

    def predict_signal(self, indicators: Dict[str, Any]) -> Dict[str, float]:
        """Return BUY/SELL probabilities, confidence and expected RR."""
        if self.model is None and not self._load_model():
            return {
                "buy_probability": 50.0,
                "sell_probability": 50.0,
                "confidence": 0.0,
                "expected_rr": round(self.expected_rr, 4),
            }
        probabilities = self.model.predict_proba([self._feature_row(indicators)])[0]
        classes = list(self.model.classes_)
        buy = float(probabilities[classes.index("BUY")]) if "BUY" in classes else 0.0
        sell = float(probabilities[classes.index("SELL")]) if "SELL" in classes else 0.0
        total = buy + sell or 1.0
        buy_pct = buy / total * 100.0
        sell_pct = sell / total * 100.0
        confidence = abs(buy_pct - sell_pct)
        return {
            "buy_probability": round(buy_pct, 2),
            "sell_probability": round(sell_pct, 2),
            "confidence": round(confidence, 2),
            "expected_rr": round(self.expected_rr, 4),
        }


AITradingPredictor = AIPredictor

_default_predictor = AIPredictor()


def train_model(history: Any = None, model_path: Optional[str] = None,
                n_estimators: int = 100) -> Dict[str, Any]:
    """Train and persist the default trading predictor."""
    return _default_predictor.train_model(history, model_path, n_estimators)


def predict_signal(indicators: Dict[str, Any]) -> Dict[str, float]:
    """Predict a signal using the automatically loaded default model."""
    return _default_predictor.predict_signal(indicators)
