import unittest

from decision_engine import DecisionEngine


class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine({
            "decision_engine": {
                "min_probability": 92,
                "structure_weight": 0.40,
                "momentum_weight": 0.35,
                "risk_weight": 0.25,
            }
        })

    def test_strong_bullish_confluence_reaches_threshold(self):
        result = self.engine.evaluate(
            latest={"rsi": 80, "macd_line": 2, "macd_signal": 1, "adx": 40, "plus_di": 30, "minus_di": 10},
            technical_signal="BUY",
            smart_money={
                "trend_structure": "bullish",
                "bos": {"direction": "bullish", "detected": True},
                "choch": {"direction": "bullish", "detected": True},
                "liquidity_sweep": {"direction": "bullish", "detected": True},
                "bullish_order_block": {"detected": True},
                "bullish_fvg": {"detected": True},
            },
            timeframe_analysis={
                "h4_h1_aligned": True,
                "rules": {"h4": "bullish"},
            },
        )
        self.assertEqual(result["signal"], "BUY")
        self.assertGreaterEqual(result["buy_probability"], 92)
        self.assertIn("reasons", result)

    def test_higher_timeframe_conflict_blocks_decision(self):
        result = self.engine.evaluate(
            latest={"rsi": 70, "macd_line": 2, "macd_signal": 1, "adx": 30, "plus_di": 25, "minus_di": 10},
            technical_signal="BUY",
            smart_money={"trend_structure": "bullish"},
            timeframe_analysis={
                "h4_h1_aligned": False,
                "rules": {"h4": "bearish"},
            },
        )
        self.assertEqual(result["signal"], "HOLD")
        self.assertTrue(any("H4" in reason for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
