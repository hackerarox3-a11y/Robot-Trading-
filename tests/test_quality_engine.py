import unittest

from quality_engine import QualityEngine


class QualityEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = QualityEngine({"quality_engine": {"minimum_score": 80}})
        self.scores = {"ema": 1, "rsi": 1, "macd": 1, "adx": 1, "volume": 1}
        self.latest = {
            "close": 100,
            "adx": 40,
            "atr": 1,
            "volume": 200,
            "avg_volume": 100,
            "rsi": 65,
            "macd_line": 2,
            "macd_signal": 1,
        }

    def test_premium_quality_is_accepted(self):
        result = self.engine.evaluate(
            self.latest,
            self.scores,
            "BUY",
            smart_money={"confidence": 1.0},
            timeframe_analysis={"h4_h1_aligned": True},
            risk_context={"spread_ok": True, "news_safe": True, "spread_pips": 1, "max_spread_pips": 10},
        )
        self.assertEqual(result["quality_level"], "Premium")
        self.assertTrue(result["accepted"])
        self.assertEqual(len(result["components"]), 9)

    def test_low_quality_is_refused(self):
        result = self.engine.evaluate(
            {"close": 100, "adx": 10, "atr": 0, "rsi": 50, "macd_line": 0, "macd_signal": 1},
            {"ema": -1, "rsi": -1},
            "BUY",
            timeframe_analysis={"h4_h1_aligned": False},
            risk_context={"spread_ok": False, "news_safe": False},
        )
        self.assertEqual(result["quality_level"], "Refuse")
        self.assertFalse(result["accepted"])


if __name__ == "__main__":
    unittest.main()
