import unittest
from pathlib import Path

from rotation_radar.v4d_simulation_account import (
    DEFAULT_STATE,
    buy_position,
    sell_position,
)


class V4DSimulationAccountTests(unittest.TestCase):
    def test_daily_workflow_uses_simulation_state_only(self):
        workflow = Path(
            ".github/workflows/v4d-dashboard-worker.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("data/formal_v4d_simulation_state.json", workflow)
        self.assertNotIn("data/formal_v4d_actual_trade_state.json", workflow)
        self.assertNotIn("v4d_sheet_sync", workflow)

    def test_whole_share_buy_leaves_insufficient_cash(self):
        state = dict(DEFAULT_STATE, cash=90.0, transactions=[], position=None)
        buy_position(
            state,
            trade_date="2026-08-05",
            ticker="0001",
            name="測試股",
            price=33.0,
            signal_date="2026-08-04",
            signal_close=32.0,
            reason="test",
        )
        self.assertEqual(state["position"]["shares"], 2)
        self.assertGreaterEqual(state["cash"], 0)
        self.assertLess(state["cash"], 33.0 * 1.001855)

    def test_round_trip_deducts_buy_and_sell_costs(self):
        state = dict(DEFAULT_STATE, cash=7000000.0, transactions=[], position=None)
        buy_position(
            state,
            trade_date="2026-08-05",
            ticker="3413",
            name="京鼎",
            price=321.5,
            signal_date="2026-08-04",
            signal_close=321.5,
            reason="test",
        )
        sell_position(
            state,
            trade_date="2026-08-10",
            price=307.5,
            reason="test exit",
        )
        self.assertIsNone(state["position"])
        self.assertLess(state["cash"], 7000000.0)
        self.assertLess(state["transactions"][-1]["realized_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
