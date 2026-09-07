import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from rotation_radar.base_cycle_daily_report import load_state, render_html, update_state
from rotation_radar.base_cycle_daily_report import load_official_prices_and_turnover


class BaseCycleDailyReportTests(unittest.TestCase):
    def test_official_display_preserves_price_lineage(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            (cache / 'official_recent_full_market.csv.gz').touch()
            row = {'date': '2026-09-07', 'ticker': '2327', 'name': '國巨',
                   'market': 'TWSE', 'close': 589, 'turnover_value': 1000,
                   'source_hash': 'a' * 64, 'source_url': 'official'}
            with patch('rotation_radar.base_cycle_daily_report.pd.read_csv', return_value=pd.DataFrame([row])):
                display, _ = load_official_prices_and_turnover(source_repo=cache,
                    target=pd.Timestamp('2026-09-07'), current=pd.DataFrame(), source_cache=cache, offline=True)
            self.assertEqual(display.iloc[0].source_hash, 'a' * 64)
            self.assertEqual(display.iloc[0].source_url, 'official')

    def top10(self, tickers):
        return pd.DataFrame([
            {
                "final_rank": index + 1,
                "ticker": ticker,
                "name": f"Name {ticker}",
                "display_close": 50.0 + index,
                "normalized_position": float(index),
                "range_pct": 30.0,
                "window_low": 40.0,
                "window_high": 70.0,
            }
            for index, ticker in enumerate(tickers)
        ])

    def test_only_first_top3_appearance_is_new(self):
        state = load_state(Path("does-not-exist.json"))
        state, first_new, _ = update_state(
            state,
            report_date="2026-07-22",
            top10=self.top10(["A", "B", "C", "D"]),
            raw_close_map={"A": 50, "B": 100, "C": 150, "D": 80},
            trading_dates=["2026-07-22"],
        )
        self.assertEqual([item["ticker"] for item in first_new], ["A", "B", "C"])
        state, second_new, registry = update_state(
            state,
            report_date="2026-07-23",
            top10=self.top10(["A", "E", "F", "B"]),
            raw_close_map={"A": 55, "B": 90, "C": 165, "E": 70, "F": 90},
            trading_dates=["2026-07-22", "2026-07-23"],
        )
        self.assertEqual([item["ticker"] for item in second_new], ["E", "F"])
        a = next(item for item in registry if item["ticker"] == "A")
        self.assertEqual(a["elapsed_trading_days"], 2)
        self.assertAlmostEqual(a["return_pct"], 10.0)

    def test_rerun_same_date_keeps_new_top3_section(self):
        state = load_state(Path("does-not-exist.json"))
        top = self.top10(["A", "B", "C"])
        state, _, _ = update_state(state, report_date="2026-07-22", top10=top, raw_close_map={"A": 50, "B": 100, "C": 150}, trading_dates=["2026-07-22"])
        state, new, _ = update_state(state, report_date="2026-07-22", top10=top, raw_close_map={"A": 50, "B": 100, "C": 150}, trading_dates=["2026-07-22"])
        self.assertEqual({item["ticker"] for item in new}, {"A", "B", "C"})

    def test_html_has_three_sections(self):
        html = render_html(pd.Timestamp("2026-07-22"), pd.Timestamp("2026-07-16"), self.top10(["A", "B", "C"]), [], [])
        self.assertIn("第一部分｜今日前十名", html)
        self.assertIn("第二部分｜今日首次進入前三名", html)
        self.assertIn("第三部分｜歷來前三名總追蹤", html)


if __name__ == "__main__":
    unittest.main()
