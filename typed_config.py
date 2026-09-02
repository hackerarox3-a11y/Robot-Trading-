"""Typed, validated application configuration built with stdlib dataclasses."""
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
BROKERS = {"mt5", "deriv", "both"}
MODES = {"PAPER", "SIMULATION", "LIVE"}

class ConfigurationError(ValueError):
    pass

@dataclass
class BrokerConfig:
    enabled: bool = True
    symbols: List[str] = field(default_factory=list)

@dataclass
class MT5Config:
    path: str = ""

@dataclass
class DerivConfig:
    ws_url: str = "wss://ws.derivws.com/websockets/v3"

@dataclass
class TradingConfig:
    enabled: bool = True
    mode: str = "SIMULATION"
    scan_interval: int = 15
    max_positions: int = 1
    symbols: List[str] = field(default_factory=list)
    timeframe: str = "M5"
    max_spread: float = 30.0
    slippage: int = 20
    retries: int = 3
    timeout: float = 30.0

@dataclass
class RiskConfig:
    risk_per_trade: float = 1.0
    max_daily_loss: float = 8.0
    max_drawdown: float = 12.0
    max_consecutive_losses: int = 4
    max_daily_trades: int = 20
    atr_multiplier: float = 1.5

@dataclass
class StrategyConfig:
    minimum_quality: float = 80.0
    minimum_confidence: float = 92.0
    indicator_weights: Dict[str, float] = field(default_factory=dict)

@dataclass
class NewsConfig:
    enabled: bool = True
    avoid_high_impact: bool = True
    avoid_medium_impact: bool = True

@dataclass
class TelegramConfig:
    enabled: bool = False

@dataclass
class DatabaseConfig:
    path: str = "trading.db"

@dataclass
class AIConfig:
    enabled: bool = True
    model_path: str = "ai_predictor_model.pkl"

@dataclass
class ApplicationConfig:
    active_broker: str
    trading: TradingConfig
    risk: RiskConfig
    strategy: StrategyConfig
    news: NewsConfig
    telegram: TelegramConfig
    deriv: DerivConfig
    mt5: MT5Config
    database: DatabaseConfig
    ai: AIConfig
    brokers: Dict[str, BrokerConfig]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationConfig":
        trading_raw = dict(raw.get("trading", {})); risk_raw = dict(raw.get("risk_management", {}))
        strategy_weights = dict(raw.get("strategy_weights", {}))
        decision = dict(raw.get("decision_engine", {})); quality = dict(raw.get("quality_engine", {}))
        active = str(raw.get("active_broker", raw.get("broker", "both"))).lower()
        trading_mode = str(trading_raw.get("mode", "SIMULATION")).upper()
        brokers = {name: BrokerConfig(**{k:v for k,v in dict(value).items() if k in ("enabled","symbols")}) for name,value in dict(raw.get("brokers", {})).items()}
        result = cls(
            active_broker=active,
            trading=TradingConfig(enabled=bool(trading_raw.get("enabled", True)), mode=trading_mode, scan_interval=int(raw.get("timing", {}).get("scan_interval_seconds", 15)), max_positions=int(trading_raw.get("max_open_positions", 1)), symbols=list(trading_raw.get("symbols", [])), timeframe=str(trading_raw.get("timeframe", "M5")).upper(), max_spread=float(trading_raw.get("max_spread_pips", 30.0)), slippage=int(trading_raw.get("deviation_slippage", 20)), retries=int(trading_raw.get("retries", 3)), timeout=float(trading_raw.get("timeout", 30.0))),
            risk=RiskConfig(risk_per_trade=float(risk_raw.get("risk_per_trade", risk_raw.get("max_risk_per_trade_pct", 1.0))), max_daily_loss=float(risk_raw.get("max_daily_loss", risk_raw.get("max_daily_loss_pct", 8.0))), max_drawdown=float(risk_raw.get("max_drawdown", risk_raw.get("max_session_drawdown_pct", 12.0))), max_consecutive_losses=int(risk_raw.get("max_consecutive_losses", 4)), max_daily_trades=int(risk_raw.get("max_daily_trades", 20)), atr_multiplier=float(risk_raw.get("atr_multiplier", raw.get("stop_loss_take_profit", {}).get("atr_sl_multiplier", 1.5)))),
            strategy=StrategyConfig(minimum_quality=float(quality.get("minimum_score", 80.0)), minimum_confidence=float(decision.get("min_probability", 92.0)), indicator_weights=strategy_weights),
            news=NewsConfig(**{k:v for k,v in dict(raw.get("news_filter", {})).items() if k in ("enabled","avoid_high_impact","avoid_medium_impact")}),
            telegram=TelegramConfig(enabled=bool(raw.get("telegram", {}).get("enabled", False))),
            deriv=DerivConfig(ws_url=str(raw.get("deriv", {}).get("ws_url", DerivConfig.ws_url))),
            mt5=MT5Config(path=str(raw.get("mt5", {}).get("path", ""))),
            database=DatabaseConfig(path=str(raw.get("database", {}).get("path", "trading.db"))),
            ai=AIConfig(enabled=bool(raw.get("ai", {}).get("enabled", True)), model_path=str(raw.get("ai", {}).get("model_path", "ai_predictor_model.pkl"))),
            brokers=brokers,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if self.active_broker not in BROKERS: raise ConfigurationError("invalid broker: %s" % self.active_broker)
        if self.trading.mode not in MODES: raise ConfigurationError("invalid trading mode: %s" % self.trading.mode)
        if self.trading.timeframe not in TIMEFRAMES: raise ConfigurationError("invalid timeframe: %s" % self.trading.timeframe)
        if self.trading.max_positions <= 0: raise ConfigurationError("max_positions must be positive")
        if self.trading.scan_interval <= 0: raise ConfigurationError("scan_interval must be positive")
        if self.risk.risk_per_trade < 0 or self.risk.risk_per_trade > 5: raise ConfigurationError("risk_per_trade must be between 0 and 5")
        if self.risk.max_daily_loss < 0 or self.risk.max_drawdown < 0: raise ConfigurationError("loss and drawdown limits cannot be negative")
        if self.risk.max_consecutive_losses <= 0 or self.risk.max_daily_trades <= 0: raise ConfigurationError("risk counters must be positive")
        if self.risk.atr_multiplier <= 0: raise ConfigurationError("ATR multiplier must be positive")
        if self.trading.max_spread < 0 or self.trading.slippage < 0 or self.trading.retries < 0 or self.trading.timeout <= 0: raise ConfigurationError("invalid execution settings")
        if not 0 <= self.strategy.minimum_quality <= 100 or not 0 <= self.strategy.minimum_confidence <= 100: raise ConfigurationError("strategy thresholds must be between 0 and 100")

def load_typed_config(path: str) -> ApplicationConfig:
    with Path(path).open(encoding="utf-8") as config_file:
        return ApplicationConfig.from_dict(json.load(config_file))

def validate_config(path: str) -> ApplicationConfig:
    return load_typed_config(path)
