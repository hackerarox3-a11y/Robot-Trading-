import json
import os
import unittest
from pathlib import Path

from deriv_connector import DerivConnector


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
        self.assertEqual(len(self.config["brokers"]["mt5"]["symbols"]), 5)

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


if __name__ == "__main__":
    unittest.main()