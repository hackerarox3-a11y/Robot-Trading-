# -*- coding: utf-8 -*-
"""
Tests des correctifs P0/P1 : sizing sans plancher dangereux, faisabilite,
protections SL/TP et suivi par symbole.
"""

import json
import unittest
from pathlib import Path

from utils import feasibility_lot, spread_ok, protected_sl_tp
from risk_manager import RiskManager

ROOT = Path(__file__).resolve().parents[1]


def build_rm(balance: float = 100.0) -> RiskManager:
    """RiskManager construit sur la vraie config.json."""
    with (ROOT / "config.json").open(encoding="utf-8") as f:
        cfg = json.load(f)
    return RiskManager(cfg, {"balance": balance, "equity": balance,
                             "currency": "USD"})


class SizingFeasibilityTests(unittest.TestCase):
    def test_small_account_raw_lot_below_config_min(self):
        rm = build_rm(100.0)
        raw = rm.calculate_lot_size(40, 10.0, respect_min=False)
        # Risque cible 1% => (100*0.01)/(40*10)=0.0025 ; config min=0.35
        self.assertLess(raw, rm.min_lot)

    def test_respect_min_keeps_floor_behavior(self):
        rm = build_rm(100.0)
        clamped = rm.calculate_lot_size(40, 10.0)
        self.assertGreaterEqual(clamped, rm.min_lot)

    def test_feasibility_lot_skips_impossible_risk(self):
        self.assertIsNone(feasibility_lot(0.0025, 0.35, 1.0, 0.05))
        ok = feasibility_lot(2.337, 0.35, 10.0, 0.05)
        self.assertIsNotNone(ok)
        self.assertAlmostEqual(ok, 2.35, delta=1e-9)

    def test_unregister_never_negative(self):
        rm = build_rm()
        rm.unregister_symbol_position("XAUUSD")  # jamais enregistre
        self.assertEqual(rm.daily_positions_per_symbol.get("XAUUSD", 0), 0)


class TradeTrackingTests(unittest.TestCase):
    def test_record_trade_with_symbol_updates_daily_map(self):
        rm = build_rm()
        rm.register_symbol_position("EURUSD")
        rm.record_trade(-3.0, "EURUSD")
        self.assertEqual(rm.daily_symbol_pnl["EURUSD"], -3.0)
        self.assertEqual(rm.daily_symbol_trades["EURUSD"], 1)
        self.assertEqual(rm.daily_positions_per_symbol["EURUSD"], 1)
        rm.unregister_symbol_position("EURUSD")
        self.assertEqual(rm.daily_positions_per_symbol["EURUSD"], 0)

    def test_set_total_exposure_flows_into_report_checks(self):
        rm = build_rm()
        rm.set_total_exposure(1500.0)
        self.assertEqual(rm.current_total_exposure, 1500.0)


class ProtectionHelpersTests(unittest.TestCase):
    def test_spread_ok_thresholds(self):
        self.assertTrue(spread_ok(None, None, 10.0, 0.0001))
        ok, s = spread_ok(1.10000, 1.10015, 2.0, 0.0001)
        self.assertTrue(ok)
        ok2, s2 = spread_ok(1.10000, 1.10120, 2.0, 0.0001)
        self.assertFalse(ok2)

    def test_protected_sl_tp_buy_enforces_min_distance_and_digits(self):
        out = protected_sl_tp(100.0, "BUY", 0.5, 1.0, 2, min_stop_distance=2.0)
        self.assertAlmostEqual(out["sl"], 98.0)
        self.assertAlmostEqual(out["tp"], 102.0)

    def test_protected_sl_tp_sell_mirror(self):
        out = protected_sl_tp(50.12345, "SELL", 1.0, 2.0, 3)
        self.assertAlmostEqual(out["sl"], 51.123)
        self.assertAlmostEqual(out["tp"], 48.123)


if __name__ == "__main__":
    unittest.main()