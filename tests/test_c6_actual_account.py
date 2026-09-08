import unittest

from rotation_radar.c6_actual_account import value_account, evaluate_exit_observation, holding_history_observation


class ActualHistoryTests(unittest.TestCase):
    def observe(self, rows, complete=True):
        return holding_history_observation(ticker='2327', entry_date='2026-09-04', as_of='2026-09-07',
            official_rows=rows, trading_dates=['2026-09-04', '2026-09-07'], calendar_complete=complete)

    def test_missing_quote_does_not_shorten_td_or_invent_peak(self):
        result = self.observe([{'ticker': '2327', 'date': '2026-09-07', 'close': 589, 'source_hash': 'a' * 64}])
        self.assertEqual(result['holding_td'], 2)
        self.assertIsNone(result['raw_close_high'])
        self.assertEqual(result['missing_dates'], ['2026-09-04'])

    def test_complete_raw_high_is_not_exit_authority(self):
        result = self.observe([{'ticker': '2327', 'date': d, 'close': p, 'source_hash': 'a' * 64}
                               for d, p in [('2026-09-04', 600), ('2026-09-07', 589)]])
        self.assertEqual(result['raw_close_high'], 600)
        self.assertFalse(result['exit_basis_ready'])

    def test_calendar_failure_does_not_become_weekday_estimate(self):
        self.assertIsNone(self.observe([], False)['holding_td'])


class ActualExitObservationTests(unittest.TestCase):
    def test_missing_peak_does_not_become_hold(self):
        result = evaluate_exit_observation(current_return=.05, holding_td=10, peak_return=None, macro_triple=None)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['unknown'], ['activated_peak_drawdown'])

    def test_loss_and_td60_are_observations_not_trades(self):
        result = evaluate_exit_observation(current_return=-.12, holding_td=60, peak_return=.1, macro_triple=None)
        self.assertIn('hard_loss_guard', result['triggered'])
        self.assertIn('maximum_holding_td', result['triggered'])
        self.assertFalse(result['actual_trades_changed'])

    def test_macro_required_only_when_profit_qualifies(self):
        result = evaluate_exit_observation(current_return=.15, holding_td=10, peak_return=.18, macro_triple=None)
        self.assertEqual(result['unknown'], ['macro_triple'])

    def test_activated_drawdown(self):
        result = evaluate_exit_observation(current_return=.1, holding_td=20, peak_return=.3, macro_triple=False)
        self.assertIn('activated_peak_drawdown', result['triggered'])


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
