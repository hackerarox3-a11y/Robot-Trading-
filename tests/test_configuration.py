import json
import os
import unittest
from pathlib import Path

from deriv_connector import DerivConnector
from main import TradingBot


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "config.json").open(encoding="utf-8") as config_file:
            cls.config = json.load(config_file)

    def test_config_contains_broker_symbol_lists(self):
        self.assertEqual(
            self.config["brokers"]["deriv"]["symbols"],
            ["R_75", "R_100", "BOOM1000", "CRASH1000", "frxEURUSD"],
        )

    def test_xau_symbol_and_strategy_weights_are_configured(self):
        self.assertIn("XAUUSDm", self.config["brokers"]["mt5"]["symbols"])
        weights = self.config["strategy_weights"]
        total_weight = sum(
            weights[name]
            for name in (
                "trend_following", "rsi_reversal", "macd_crossover",
                "bollinger_bounce", "adx_filter", "stochastic",
                "divergence", "ichimoku", "pivot_points",
            )
        )
        self.assertAlmostEqual(total_weight, 1.0, places=6)
        self.assertEqual(
            self.config["brokers"]["mt5"]["symbols"],
            ["XAUUSDm", "EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm"],
        )

    def test_deriv_connector_uses_deriv_symbols_and_environment_token(self):
        previous_token = os.environ.get("DERIV_API_TOKEN")
        os.environ["DERIV_API_TOKEN"] = "test-token"
        try:
            connector = DerivConnector(self.config)
            self.assertEqual(connector.symbols, self.config["brokers"]["deriv"]["symbols"])
            self.assertEqual(connector.api_token, "test-token")
        finally:
            if previous_token is None:
                os.environ.pop("DERIV_API_TOKEN", None)
            else:
                os.environ["DERIV_API_TOKEN"] = previous_token

    def test_dry_run_does_not_attempt_broker_connection_without_credentials(self):
        previous_token = os.environ.get("DERIV_API_TOKEN")
        os.environ.pop("DERIV_API_TOKEN", None)
        self.config["deriv"]["api_token"] = ""
        try:
            bot = TradingBot(str(ROOT / "config.json"), dry_run=True, broker="deriv")
            self.assertEqual(bot.connectors, {})
        finally:
            if previous_token is None:
                os.environ.pop("DERIV_API_TOKEN", None)
            else:
                os.environ["DERIV_API_TOKEN"] = previous_token
            self.config["deriv"]["api_token"] = ""


if __name__ == "__main__":
    unittest.main()