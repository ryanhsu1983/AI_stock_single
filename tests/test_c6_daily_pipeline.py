import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date
from unittest.mock import patch

import pandas as pd

from rotation_radar.c6_daily_pipeline import advance_account, rank_score0, load_official_0050, actual_history_payload


class C6DailyPipelineTests(unittest.TestCase):
    @patch('rotation_radar.c6_daily_pipeline.next_trading_day', return_value=date(2026, 9, 10))
    def test_withdrawal_sale_reduces_basis_without_false_loss(self, next_day):
        day = pd.Timestamp('2026-09-09')
        payload = {'slots':[{'slot_id':1,'ticker':'3653','shares':10000,'position_cost':1000000,'slot_cash':1000}],
            'ledger_rows':[{'account_date':'2026-09-08','event_type':'buy','slot_id':1,'ticker':'3653'}]}
        official = pd.DataFrame([{'date':day,'ticker':'3653','close':100}])
        adjusted = pd.DataFrame([{'date':pd.Timestamp(d),'ticker':'3653','adjusted_analysis_close':100} for d in ('2026-09-08','2026-09-09')])
        slots, ledger, cash, blockers, pending = advance_account(payload, day, pd.DataFrame(columns=['ticker']), official, adjusted)
        self.assertEqual(blockers, [])
        self.assertEqual(pending, [])
        self.assertEqual(slots[0]['shares'], 9250)
        self.assertEqual(slots[0]['position_cost'], 925000)
        self.assertAlmostEqual(slots[0]['shares']*100/slots[0]['position_cost']-1, 0)
        sale = next(r for r in ledger if r['event_type']=='withdrawal_sale')
        self.assertEqual(sale['allocated_cost'],75000)
        self.assertLess(sale['realized_pnl'],0)  # Only actual sale costs, not the withdrawal principal.
        self.assertGreater(sale['realized_pnl'],-400)

    @patch('rotation_radar.c6_daily_pipeline.fetch_twse_calendar', return_value=(set(), {date(2026, 1, 1)}))
    @patch('rotation_radar.c6_daily_pipeline.urlopen')
    def test_incomplete_past_month_refreshes_but_complete_month_is_reused(self, fetch, calendar):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            def data(days):
                return {'data': [[d, '', '1000', '', '', '', '100'] for d in days]}
            (root / '0050-2026-07.json').write_text(json.dumps(data(['115/07/30'])), encoding='utf-8')
            (root / '0050-2026-08.json').write_text(json.dumps(data(['115/08/03'])), encoding='utf-8')
            fetch.return_value.__enter__.return_value.read.return_value = json.dumps(data(['115/07/30', '115/07/31'])).encode()
            result = load_official_0050(pd.Timestamp('2026-07-30'), pd.Timestamp('2026-08-03'), root)
            self.assertEqual(len(result), 3)
            fetch.assert_called_once()

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

    @patch('rotation_radar.c6_daily_pipeline.fetch_twse_calendar', return_value=(set(), {date(2026, 1, 1)}))
    @patch('rotation_radar.c6_daily_pipeline.urlopen')
    def test_current_month_cache_refreshes_for_new_session(self, fetch, calendar):
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
