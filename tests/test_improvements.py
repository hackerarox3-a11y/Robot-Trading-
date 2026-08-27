# -*- coding: utf-8 -*-
"""
Tests unitaires des ameliorations apportees au bot.
Ces tests ne requièrent aucune connexion reseau ni MetaTrader installe.
"""

import json
import logging
import unittest

from main import deep_merge, normalize_lot, setup_logging


class NormalizeLotTests(unittest.TestCase):
    def test_rounds_to_lot_step(self):
        result = normalize_lot(0.37, 0.01, 10.0, 0.05)
        # Comparison robuste aux flottants (0.35 binaire != 0.35 decimal)
        self.assertAlmostEqual(result, 0.35, delta=0.001)
        result2 = normalize_lot(0.383, 0.01, 10.0, 0.01)
        self.assertAlmostEqual(result2, 0.38, delta=0.001)

    def test_clamps_to_min_and_max(self):
        self.assertEqual(normalize_lot(0.001, 0.01, 1.0, 0.01), 0.01)
        self.assertEqual(normalize_lot(500.0, 0.01, 1.0, 0.01), 1.0)

    def test_zero_step_falls_back_without_crash(self):
        # Avant l'amelioration: ZeroDivisionError
        result = normalize_lot(0.37, 0.01, 10.0, 0)
        self.assertGreaterEqual(result, 0.01)
        self.assertLessEqual(result, 10.0)

    def test_invalid_step_string_falls_back(self):
        result = normalize_lot(0.37, 0.01, 10.0, "n/a")
        self.assertGreaterEqual(result, 0.01)


class DeepMergeTests(unittest.TestCase):
    def test_nested_merge_does_not_mutate_base(self):
        base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
        override = {"a": {"y": 3, "z": 4}}
        merged = deep_merge(base, override)
        self.assertEqual(merged, {"a": {"x": 1, "y": 3, "z": 4}, "b": [1, 2]})
        self.assertEqual(base["a"]["y"], 2)  # base intact


class SetupLoggingTests(unittest.TestCase):
    LOG_FILE = "test_log_rotation_tmp.log"

    @classmethod
    def tearDownClass(cls):
        import os
        if os.path.exists(cls.LOG_FILE):
            os.remove(cls.LOG_FILE)

    def test_rotating_file_handler_used_by_default(self):
        cfg = {"logging": {"level": "INFO", "log_to_file": True,
                           "log_file": self.LOG_FILE}}
        root = logging.getLogger()
        old_handlers = list(root.handlers)
        try:
            setup_logging(cfg)
            handlers_after = [
                h for h in root.handlers if h not in old_handlers
            ]
            from logging.handlers import RotatingFileHandler
            rotating = [h for h in handlers_after
                        if isinstance(h, RotatingFileHandler)]
            self.assertTrue(rotating, "RotatingFileHandler attendu par defaut")
            self.assertEqual(rotating[0].maxBytes, 10 * 1024 * 1024)
        finally:
            for h in list(root.handlers):
                if h not in old_handlers:
                    h.close()
                    root.removeHandler(h)


class ConfigJsonTests(unittest.TestCase):
    def test_close_positions_on_stop_key_exists(self):
        with open("config.json", encoding="utf-8") as f:
            config = json.load(f)
        self.assertTrue(config["trading"].get("close_positions_on_stop", False))


if __name__ == "__main__":
    unittest.main()