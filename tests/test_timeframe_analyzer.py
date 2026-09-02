import json
import unittest
from pathlib import Path

from timeframe_analyzer import TimeframeAnalyzer


ROOT = Path(__file__).resolve().parents[1]


class StubAnalyzer(TimeframeAnalyzer):
    def __init__(self, directions):
        super().__init__(connector=None, config={"timeframe_analyzer": {"enabled": True}})
        self.directions = directions

    def analyze_timeframe(self, symbol, timeframe):
        return {
            "timeframe": timeframe,
            "available": True,
            "direction": self.directions.get(timeframe, "none"),
        }


class TimeframeAnalyzerTests(unittest.TestCase):
    def test_h4_and_h1_must_align(self):
        analyzer = StubAnalyzer({"H4": "bullish", "H1": "bearish", "M15": "bullish", "M1": "bullish"})
        result = analyzer.combine_timeframes("EURUSDm", "BUY")
        self.assertFalse(result["allowed"])
        self.assertFalse(result["h4_h1_aligned"])

    def test_aligned_higher_timeframes_allow_signal(self):
        analyzer = StubAnalyzer({"H4": "bullish", "H1": "bullish", "M15": "bullish", "M1": "bullish"})
        result = analyzer.combine_timeframes("EURUSDm", "BUY")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["direction"], "BUY")

    def test_config_declares_timeframe_analyzer(self):
        with (ROOT / "config.json").open(encoding="utf-8") as config_file:
            config = json.load(config_file)
        self.assertTrue(config["timeframe_analyzer"]["enabled"])
        self.assertEqual(config["timeframe_analyzer"]["bars_per_timeframe"], 300)


if __name__ == "__main__":
    unittest.main()
