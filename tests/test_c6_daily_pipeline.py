import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date
from unittest.mock import patch

import pandas as pd

from rotation_radar.c6_daily_pipeline import advance_account, rank_score0, load_official_0050, actual_history_payload


class C6DailyPipelineTests(unittest.TestCase):
    @patch('rotation_radar.c6_daily_pipeline.fetch_twse_calendar', return_value=(set(), {date(2026, 7, 2)}))
    def test_actual_history_requires_complete_benchmark_sessions(self, calendar):
        frame = pd.DataFrame([
            {'date': pd.Timestamp('2026-07-01'), 'ticker': '0050', 'close': 100, 'source_hash': 'a' * 64},
            {'date': pd.Timestamp('2026-07-01'), 'ticker': '2327', 'close': 500, 'source_hash': 'b' * 64},
        ])
        result = actual_history_payload(frame, pd.Timestamp('2026-07-02'))
        self.assertTrue(result['calendar_complete'])
        self.assertEqual(result['official_rows'][0]['source_hash'], 'b' * 64)
        self.assertFalse(actual_history_payload(frame, pd.Timestamp('2026-07-03'))['calendar_complete'])

    @patch('rotation_radar.c6_daily_pipeline.urlopen')
    def test_current_month_cache_refreshes_for_new_session(self, fetch):
        with TemporaryDirectory() as folder:
            cached = Path(folder) / '0050-2026-09.json'
            cached.write_text(json.dumps({'data': [['115/09/04', '', '1000', '', '', '', '100']]}), encoding='utf-8')
            fetch.return_value.__enter__.return_value.read.return_value = json.dumps(
                {'data': [['115/09/07', '', '1100', '', '', '', '101']]}
            ).encode()
            result = load_official_0050(pd.Timestamp('2026-09-01'), pd.Timestamp('2026-09-07'), Path(folder))
            self.assertEqual(result.iloc[0]['close'], 101)
            fetch.assert_called_once()

    def test_score0_ranking_applies_gates_and_lexical_tiebreak(self):
        day = pd.Timestamp("2026-09-04")
        liquidity = pd.DataFrame([
            {"signal_date": day, "ticker": "3653", "liquidity_pass": True},
            {"signal_date": day, "ticker": "3324", "liquidity_pass": True},
            {"signal_date": day, "ticker": "2301", "liquidity_pass": True},
        ])
        features = pd.DataFrame([
            {"date": day, "ticker": "3653", "return_60d": .2, "bottom_score": 70, "launch_score": 80, "stock_rs20": .2, "sector_rs20": .1},
            {"date": day, "ticker": "3324", "return_60d": .2, "bottom_score": 70, "launch_score": 75, "stock_rs20": .1, "sector_rs20": .1},
            {"date": day, "ticker": "2301", "return_60d": .2, "bottom_score": 55, "launch_score": 90, "stock_rs20": .3, "sector_rs20": .2},
        ])
        revenue = pd.DataFrame([
            {"ticker": "3653", "monthly_revenue_yoy": .1},
            {"ticker": "3324", "monthly_revenue_yoy": .1},
            {"ticker": "2301", "monthly_revenue_yoy": .1},
        ])
        ranked = rank_score0(features, liquidity, {"3653", "3324", "2301"}, revenue, day)
        self.assertEqual(ranked.ticker.tolist(), ["3653", "3324"])
        self.assertEqual(ranked["rank"].tolist(), [1, 2])

    @patch('rotation_radar.c6_daily_pipeline.next_trading_day', return_value=date(2026, 9, 7))
    def test_three_slot_account_marks_positions_and_schedules_next_day_exit(self, next_day):
        day = pd.Timestamp("2026-09-04")
        payload = {
            "cash": 100,
            "slots": [{"slot_id": 1, "ticker": "3653", "shares": 10, "position_cost": 1000, "raw_close": 100}],
            "ledger_rows": [{
                "account_date": "2026-09-03", "event_sequence": 1, "slot_id": 1,
                "event_type": "buy", "ticker": "3653", "cash_after": 100,
            }],
        }
        official = pd.DataFrame([{"date": day, "ticker": "3653", "close": 85, "market": "TWSE"}])
        adjusted = pd.DataFrame([
            {"date": pd.Timestamp("2026-09-03"), "ticker": "3653", "adjusted_analysis_close": 100},
            {"date": day, "ticker": "3653", "adjusted_analysis_close": 85},
        ])
        slots, ledger, cash, blockers, pending = advance_account(
            payload, day, pd.DataFrame(columns=["ticker"]), official, adjusted
        )
        self.assertEqual(blockers, [])
        self.assertEqual(slots[0]["raw_close"], 85)
        self.assertEqual(ledger[-1]["event_type"], "daily_mark")
        self.assertEqual(pending[0]["action"], "sell")
        self.assertEqual(pending[0]["reason"], "hard_loss_guard")
        self.assertEqual(cash, 100)


if __name__ == "__main__":
    unittest.main()
