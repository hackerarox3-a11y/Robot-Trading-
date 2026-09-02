import unittest

import numpy as np

from smart_money import SmartMoneyAnalyzer


class SmartMoneyTests(unittest.TestCase):
    def test_returns_required_structure(self):
        close = np.array([100 + i * 0.2 for i in range(80)], dtype=float)
        candles = {
            "open": close - 0.05,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
        }
        result = SmartMoneyAnalyzer({}).analyze(candles)
        for key in (
            "trend_structure", "bos", "choch", "bullish_order_block",
            "bearish_order_block", "bullish_fvg", "bearish_fvg",
            "liquidity_sweep", "confidence",
        ):
            self.assertIn(key, result)
        self.assertIn(result["trend_structure"], ("bullish", "bearish", "ranging"))
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_detects_bullish_fvg(self):
        base = np.arange(100.0, 112.0)
        candles = {
            "open": base - 0.05,
            "high": base + 0.15,
            "low": base - 0.15,
            "close": base,
        }
        candles["high"][-3:] = [110.0, 114.0, 115.0]
        candles["low"][-3:] = [109.0, 113.0, 114.0]
        result = SmartMoneyAnalyzer({"smart_money": {"swing_length": 2}}).analyze(candles)
        self.assertTrue(result["bullish_fvg"]["detected"])
        self.assertEqual(result["bullish_fvg"]["direction"], "bullish")


if __name__ == "__main__":
    unittest.main()
