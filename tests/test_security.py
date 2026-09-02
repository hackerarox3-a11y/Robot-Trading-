import os
import unittest
from pathlib import Path

from security import load_environment, validate_environment


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.names = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "DERIV_APP_ID", "DERIV_ACCESS_TOKEN")
        self.saved = {name: os.environ.get(name) for name in self.names}
        for name in self.names:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_secret_present(self):
        os.environ.update(MT5_LOGIN="1", MT5_PASSWORD="secret", MT5_SERVER="server")
        self.assertTrue(validate_environment("LIVE", "mt5")["valid"])

    def test_secret_absent(self):
        result = validate_environment("LIVE", "mt5")
        self.assertFalse(result["valid"])
        self.assertIn("MT5_PASSWORD", result["missing"])

    def test_env_example_has_no_values(self):
        example = Path(__file__).parents[1] / ".env.example"
        for line in example.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                self.assertEqual(line.split("=", 1)[1], "")

    def test_env_absent_is_safe(self):
        self.assertFalse(load_environment(str(Path("missing_security_test.env"))))

    def test_paper_and_simulation_do_not_require_secrets(self):
        self.assertTrue(validate_environment("PAPER", "mt5")["valid"])
        self.assertTrue(validate_environment("SIMULATION", "deriv")["valid"])

    def test_live_telegram_requires_secrets(self):
        result = validate_environment("LIVE", "mt5", telegram_enabled=True)
        self.assertIn("TELEGRAM_TOKEN", result["missing"])
        self.assertIn("TELEGRAM_CHAT_ID", result["missing"])


if __name__ == "__main__":
    unittest.main()