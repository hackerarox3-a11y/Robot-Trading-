import json
import os
import unittest
from pathlib import Path

from deriv_connector import DerivConnector
from main import TradingBot
from strategy_engine import normalize_weights


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
        total_weight = sum(normalize_weights(weights).values())
        self.assertAlmostEqual(total_weight, 1.0, places=6)

    def test_weight_normalization_accepts_legacy_names(self):
        normalized = normalize_weights({
            "trend_following": 2,
            "rsi_reversal": 1,
            "volume": 1,
        })
        self.assertAlmostEqual(sum(normalized.values()), 1.0, places=6)
        self.assertGreater(normalized["ema"], normalized["rsi"])
        self.assertGreater(normalized["volume"], 0)
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
        try:
            bot = TradingBot(str(ROOT / "config.json"), dry_run=True, broker="deriv")
            self.assertEqual(bot.connectors, {})
        finally:
            if previous_token is None:
                os.environ.pop("DERIV_API_TOKEN", None)
            else:
                os.environ["DERIV_API_TOKEN"] = previous_token


if __name__ == "__main__":
    unittest.main()