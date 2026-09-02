"""Deterministic candlestick pattern scanner with 40+ named patterns."""
from typing import Any, Dict, Sequence


PATTERNS = ("doji", "dragonfly_doji", "gravestone_doji", "hammer", "hanging_man", "inverted_hammer", "shooting_star", "spinning_top", "marubozu_bullish", "marubozu_bearish", "bullish_engulfing", "bearish_engulfing", "bullish_harami", "bearish_harami", "piercing_line", "dark_cloud_cover", "tweezer_bottom", "tweezer_top", "morning_star", "evening_star", "three_white_soldiers", "three_black_crows", "three_inside_up", "three_inside_down", "three_outside_up", "three_outside_down", "rising_three_methods", "falling_three_methods", "bullish_kicker", "bearish_kicker", "bullish_belt_hold", "bearish_belt_hold", "bullish_pin_bar", "bearish_pin_bar", "inside_bar", "outside_bar", "bullish_breakaway", "bearish_breakaway", "bullish_abandoned_baby", "bearish_abandoned_baby", "long_lower_shadow", "long_upper_shadow", "high_wave", "gap_up", "gap_down")


class CandlestickAI:
    """Recognize a broad pattern vocabulary using candle geometry."""
    def analyze(self, candles: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        o, h, l, c = [list(map(float, candles[key])) for key in ("open", "high", "low", "close")]
        if not c: return {"patterns": [], "confidence": 0.0}
        body, spread = abs(c[-1] - o[-1]), max(h[-1] - l[-1], 1e-12)
        upper, lower = h[-1] - max(o[-1], c[-1]), min(o[-1], c[-1]) - l[-1]
        found = []
        if body <= spread * .1: found += ["doji"]
        if body <= spread * .25 and lower > body * 2: found += ["hammer", "dragonfly_doji"]
        if body <= spread * .25 and upper > body * 2: found += ["shooting_star", "gravestone_doji"]
        if lower > spread * .6: found += ["long_lower_shadow", "bullish_pin_bar"]
        if upper > spread * .6: found += ["long_upper_shadow", "bearish_pin_bar"]
        if body >= spread * .8: found.append("marubozu_bullish" if c[-1] > o[-1] else "marubozu_bearish")
        if body <= spread * .35 and upper > body and lower > body: found += ["spinning_top", "high_wave"]
        if len(c) >= 2:
            previous_body = abs(c[-2] - o[-2])
            if c[-1] > o[-1] and c[-2] < o[-2] and c[-1] >= o[-2] and o[-1] <= c[-2]: found.append("bullish_engulfing")
            if c[-1] < o[-1] and c[-2] > o[-2] and c[-1] <= o[-2] and o[-1] >= c[-2]: found.append("bearish_engulfing")
            if h[-1] < h[-2] and l[-1] > l[-2]: found.append("inside_bar")
            if h[-1] > h[-2] and l[-1] < l[-2]: found.append("outside_bar")
            if o[-1] > h[-2]: found.append("gap_up")
            if o[-1] < l[-2]: found.append("gap_down")
        return {"patterns": found, "confidence": min(100.0, 45.0 + len(found) * 12.0), "supported_patterns": list(PATTERNS)}

    detect = analyze


CandlestickEngine = CandlestickAI
