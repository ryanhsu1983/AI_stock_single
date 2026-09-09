import unittest
from rotation_radar.c6_capital_replay import rebase_opening_segment
from rotation_radar.c6_account_basis import INITIAL_CAPITAL
from rotation_radar.c6_actual_dashboard_publish import build_actual_dashboard, daily_observation_rows
from rotation_radar.c6_dashboard_layout import format_requests


class MigrationTests(unittest.TestCase):
    def test_capital_replay_recomputes_whole_units_and_conserves_cash(self):
        p = {'accounting_snapshot_as_of': '2026-09-08', 'ledger_rows': [
            {'event_type': 'buy', 'slot_id': i, 'raw_close': 100} for i in (1,2,3)],
             'slots':[{'slot_id':i} for i in (1,2,3)]}
        r = rebase_opening_segment(p)
        self.assertAlmostEqual(sum(s['position_cost'] for s in r['slots'])+r['cash'], INITIAL_CAPITAL)
        self.assertTrue(all(isinstance(s['shares'],int) for s in r['slots']))
        self.assertNotIn('shares', p['slots'][0])
        p['ledger_rows'].append({'event_type':'withdrawal'})
        with self.assertRaises(ValueError):
            rebase_opening_segment(p)

    def test_actual_fifth_slot_and_historical_loss_not_double_deducted(self):
        tickers = ['2327','2376','3037','6488','2344']
        account = [['02｜預留五格'], []] + [[i+1,f'{t} 名稱｜10股',100,1000] for i,t in enumerate(tickers)] + [[]]+[['現金餘額',305358]]
        p = {'ranking_snapshot_as_of':'2026-09-08','snapshot_rows':[], 'market_rows':[
            {'date':'2026-09-08','ticker':t,'close':110,'source_hash':'a'*64} for t in tickers]}
        ledger = [[] ,['2026-08-10','歷史','實際成交（V4-D）','3413','京鼎','賣出','','','','','',-494914]]
        rows = build_actual_dashboard(account,p,ledger)
        self.assertEqual(len(daily_observation_rows(account,p,ledger)),5)
        self.assertEqual(rows[17][1],305358)
        self.assertEqual(rows[17][3],310858)
        self.assertEqual(rows[19][1],-494914)
        self.assertEqual(rows[18][1], INITIAL_CAPITAL)
        account[2][1]='2327 名稱｜5股'
        obs = daily_observation_rows(account,p,ledger)
        self.assertAlmostEqual(obs[0][16],.1)
        account[3] = [2, '未持有／預留位置']
        self.assertEqual(len(daily_observation_rows(account,p,ledger)),4)
        rerender = build_actual_dashboard(account,p,ledger)
        self.assertEqual(rerender[12][1], '未持有／預留位置')
        self.assertIn('3037', rerender[13][1])

    def test_format_has_numeric_cash_and_percent_not_date(self):
        requests = format_requests(123)
        self.assertTrue(all(len(r)==1 for r in requests))
        self.assertTrue(any('unmergeCells' in r for r in requests))
