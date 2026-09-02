import unittest
from typed_config import ApplicationConfig, ConfigurationError, load_typed_config


class TypedConfigTests(unittest.TestCase):
    def base(self):
        return {
            "active_broker": "mt5",
            "trading": {"timeframe": "M5", "max_open_positions": 2, "symbols": ["EURUSDm"]},
            "timing": {"scan_interval_seconds": 15},
            "risk_management": {"max_risk_per_trade_pct": 1.0, "max_daily_loss_pct": 8.0, "max_session_drawdown_pct": 12.0, "max_consecutive_losses": 4, "max_daily_trades": 20},
            "strategy_weights": {"ema": 1.0},
            "decision_engine": {"min_probability": 92.0},
            "quality_engine": {"minimum_score": 80.0},
            "brokers": {"mt5": {"enabled": True, "symbols": ["EURUSDm"]}},
        }

    def test_valid_configuration(self):
        config = ApplicationConfig.from_dict(self.base())
        self.assertEqual(config.trading.timeframe, "M5")
        self.assertEqual(config.risk.risk_per_trade, 1.0)

    def assert_invalid(self, mutate):
        values = self.base()
        mutate(values)
        with self.assertRaises(ConfigurationError):
            ApplicationConfig.from_dict(values)

    def test_invalid_risk(self):
        self.assert_invalid(lambda c: c["risk_management"].update(max_risk_per_trade_pct=-1))
        self.assert_invalid(lambda c: c["risk_management"].update(max_risk_per_trade_pct=6))

    def test_invalid_execution_and_position_limits(self):
        self.assert_invalid(lambda c: c["trading"].update(max_open_positions=0))
        self.assert_invalid(lambda c: c["trading"].update(max_spread_pips=-1))
        self.assert_invalid(lambda c: c["stop_loss_take_profit"] if False else c["risk_management"].update(atr_multiplier=0))

    def test_invalid_identifiers(self):
        self.assert_invalid(lambda c: c.update(active_broker="unknown"))
        self.assert_invalid(lambda c: c["trading"].update(mode="INVALID"))
        self.assert_invalid(lambda c: c["trading"].update(timeframe="T2"))

    def test_real_config_loads(self):
        config = load_typed_config("config.json")
        self.assertIn(config.active_broker, ("mt5", "deriv", "both"))


if __name__ == "__main__":
    unittest.main()
