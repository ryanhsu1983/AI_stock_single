import unittest

from rotation_radar.c6_actual_account import value_account


class ActualValuationTests(unittest.TestCase):
    def run_value(self, **changes):
        args = dict(holdings=[{'ticker': '2327', 'units': 4000, 'entry_date': '2026-08-31'}],
                    market_rows=[{'ticker': '2327', 'date': '2026-09-04', 'close': 562,
                                  'source_hash': 'a' * 64}],
                    as_of='2026-09-04', cash=200000, cash_confirmed=False,
                    event_coverage={'2327': {'accepted': True, 'start': '2026-08-31',
                                            'end': '2026-09-04', 'evidence_hash': 'review',
                                            'units_reconciled': 4000}})
        args.update(changes)
        return value_account(**args)

    def test_cash_estimate_never_becomes_confirmed(self):
        result = self.run_value()
        self.assertEqual(result['nav'], 2448000)
        self.assertEqual(result['status'], 'provisional_cash')
        self.assertFalse(result['actual_trades_changed'])

    def test_missing_events_blocks_nav(self):
        self.assertIsNone(self.run_value(event_coverage={})['nav'])

    def test_nan_source_is_not_authority(self):
        for missing in ('nan', 'None', '', None):
            self.assertIsNone(self.run_value(market_rows=[{
                'ticker': '2327', 'date': '2026-09-04', 'close': 562,
                'source_hash': missing}])['nav'])

    def test_stale_price_is_not_carried(self):
        self.assertIsNone(self.run_value(market_rows=[{'ticker': '2327', 'date': '2026-09-03',
                                                     'close': 562, 'source_hash': 'x'}])['nav'])

    def test_duplicate_marks_block(self):
        row = {'ticker': '2327', 'date': '2026-09-04', 'close': 562, 'source_hash': 'x'}
        self.assertIsNone(self.run_value(market_rows=[row, row])['nav'])

    def test_old_coverage_does_not_authorize_new_session(self):
        self.assertIsNone(self.run_value(as_of='2026-09-07')['nav'])

    def test_no_fractional_actual_units(self):
        with self.assertRaises(ValueError):
            self.run_value(holdings=[{'ticker': '2327', 'units': 1.5, 'entry_date': '2026-08-31'}])

    def test_empty_account_is_cash_only(self):
        self.assertEqual(self.run_value(holdings=[])['nav'], 200000)
