import unittest

from rotation_radar.c6_dashboard_publish import (
    MODEL_LOGIC,
    _append_only,
    build_dashboard_values,
    build_public_snapshot_values,
    build_trade_record_values,
    select_withdrawal_slot,
)


class C6DashboardPublishTests(unittest.TestCase):
    @staticmethod
    def _pairs(rows):
        values = {}
        for row in rows:
            if len(row) >= 2 and row[0]:
                values[row[0]] = row[1]
            if len(row) >= 4 and row[2]:
                values[row[2]] = row[3]
        return values

    def test_withdrawal_prefers_lowest_relative_return_slot(self):
        result = select_withdrawal_slot([
            {"slot_id": "A", "ticker": "1111", "shares": 1000, "raw_close": 100, "position_cost": 90_000},
            {"slot_id": "B", "ticker": "2222", "shares": 1000, "raw_close": 100, "position_cost": 110_000},
            {"slot_id": "C", "ticker": "3333", "shares": 1000, "raw_close": 100, "position_cost": 100_000},
        ])
        self.assertEqual(result["slot_id"], "B")
        self.assertEqual(result["planned_shares"], 750)
        self.assertLess(result["relative_return_pct"], 0)

    def test_dashboard_declares_research_version_and_missing_data(self):
        values = self._pairs(build_dashboard_values(
            model_version="c6-research-v1", snapshot_as_of="2026-08-30T15:00:00+08:00",
            data_status="blocked_source_or_replay_not_materialized", slots=[], notes="C6 daily source/replay pending",
        ))
        self.assertEqual(values["最新排名日期"], "2026-08-30T15:00:00+08:00")
        self.assertEqual(values["正式帳本日期"], "2026-08-30T15:00:00+08:00")
        self.assertIn("只核對到", values["資料狀態"])
        self.assertIn("暫不提供", values["目前預估"])

    def test_cash_sufficient_for_withdrawal_does_not_sell_a_slot(self):
        result = select_withdrawal_slot(
            [{"slot_id": "A", "ticker": "1111", "shares": 1000, "raw_close": 100, "position_cost": 90_000}],
            cash=80_000,
        )
        self.assertEqual(result["status"], "cash_withdrawal")
        self.assertEqual(result["planned_shares"], 0)

    def test_same_version_snapshot_is_not_overwritten(self):
        headers = ["model_version", "snapshot_as_of", "signal_date", "rank"]
        existing = [headers, ["c6-v1", "2026-08-05T15:00:00+08:00", "2026-08-05", 1]]
        additions = _append_only(
            existing, headers,
            [{"model_version": "c6-v1", "snapshot_as_of": "2026-08-05T15:00:00+08:00", "signal_date": "2026-08-05", "rank": 1}],
            ("model_version", "snapshot_as_of", "signal_date", "rank"),
        )
        self.assertEqual(additions, [])

    def test_append_only_uses_named_fields_instead_of_first_columns(self):
        headers = ["model_version", "snapshot_as_of", "data_status", "signal_date", "rank"]
        existing = [headers, ["c6-v2", "2026-08-28", "partial", "2026-08-28", 1]]
        additions = _append_only(
            existing,
            headers,
            [{"model_version": "c6-v2", "snapshot_as_of": "2026-08-28", "data_status": "partial", "signal_date": "2026-08-28", "rank": 1}],
            ("model_version", "snapshot_as_of", "signal_date", "rank"),
        )
        self.assertEqual(additions, [])

    def test_public_snapshot_contains_only_one_current_row_per_date_and_rank(self):
        values = build_public_snapshot_values([
            {"signal_date": "2026-08-28", "rank": 1, "ticker": "2301", "name": "光寶科", "score": 79.2},
            {"signal_date": "2026-08-28", "rank": 1, "ticker": "2301", "name": "光寶科", "score": 79.2},
            {"signal_date": "2026-08-28", "rank": 2, "ticker": "3653", "name": "健策", "score": 78.7},
        ])
        self.assertEqual(values[0][0], "訊號日期")
        self.assertEqual(len(values), 3)
        self.assertEqual(values[1][2:6], ["2301", "光寶科", "", 79.2])

    def test_public_snapshot_keeps_explicit_no_candidate_or_pit_blocker(self):
        values = build_public_snapshot_values([
            {"signal_date": "2026-08-05", "rank": 0, "candidate_status": "frozen_engine_explicit_no_eligible_candidate"},
            {"signal_date": "2026-08-31", "rank": 0, "candidate_status": "source_blocked_not_empty_candidate"},
        ])
        self.assertEqual(values[1][1], "無候選")
        self.assertEqual(values[2][1], "PIT待補")

    def test_dashboard_shows_latest_top3_in_plain_language(self):
        rows = build_dashboard_values(
            model_version="c6-v2",
            snapshot_as_of="2026-08-28",
            data_status="partial_rankings_only_no_whole_share_replay",
            slots=[],
            snapshot_rows=[
                {"signal_date": "2026-08-28", "rank": 1, "ticker": "2301", "name": "光寶科", "score": 79.2},
                {"signal_date": "2026-08-28", "rank": 2, "ticker": "3653", "name": "健策", "score": 78.7},
                {"signal_date": "2026-08-28", "rank": 3, "ticker": "3324", "name": "雙鴻", "score": 77.1},
            ],
        )
        top1 = next(row for row in rows if row[0] == "Top1")
        self.assertEqual(top1[:4], ["Top1", "2301 光寶科", 79.2, "已通過C6條件，當日排名第1"])
        values = self._pairs(rows)
        self.assertNotIn("data_status", values)
        flattened = "\n".join(str(cell) for row in rows for cell in row)
        self.assertNotIn("partial_rankings_only", flattened)
        self.assertNotIn("no_whole_share_replay", flattened)

    def test_dashboard_distinguishes_fewer_than_three_qualified_stocks_from_missing_data(self):
        rows = build_dashboard_values(
            model_version="c6-v2",
            snapshot_as_of="2026-09-03",
            data_status="ready",
            slots=[],
            snapshot_rows=[
                {"signal_date": "2026-09-03", "rank": 1, "ticker": "3324", "name": "雙鴻", "score": 92.3125},
            ],
        )
        top2 = next(row for row in rows if row[0] == "Top2")
        top3 = next(row for row in rows if row[0] == "Top3")
        self.assertEqual(top2[1], "無其他合格股票")
        self.assertEqual(top3[1], "無其他合格股票")

    def test_trade_record_is_readable_and_deduplicated(self):
        ledger = [
            {"account_date": "2026-08-07", "event_sequence": 1, "slot_id": 1, "event_type": "buy", "ticker": "3653", "shares": 531, "raw_close": 4380, "gross_amount": 2328105.78, "transaction_cost": 3317.55, "cash_after": 1910, "reason": "ai_bottom_launch_rank1"},
            {"account_date": "2026-08-07", "event_sequence": 2, "slot_id": 1, "event_type": "daily_mark", "ticker": "3653", "shares": 531, "raw_close": 4380, "gross_amount": 2325780, "relative_return_pct": -0.0024, "reason": "official_raw_holding_mark"},
        ]
        snapshots = [{"signal_date": "2026-08-06", "planned_execution_date": "2026-08-07", "rank": 1, "ticker": "3653", "name": "健策"}]
        values = build_trade_record_values(ledger, snapshots)
        self.assertEqual(values[0][0:6], ["日期", "槽位", "事件類型", "股票代號", "股票名稱", "動作"])
        self.assertEqual(values[1][4:6], ["健策", "買進"])
        self.assertEqual(values[1][13], "2026-08-06")
        self.assertEqual(values[1][17], "依2026-08-06收盤C6 Top1訊號，下一交易日建立第1槽部位")
        self.assertEqual(values[2][5], "續抱")
        self.assertEqual(values[2][17], "2026-08-07買入，已持有1 TD；續抱中，未觸發賣出條件")

    def test_partial_dashboard_keeps_withdrawal_schedule_but_not_a_fabricated_sale(self):
        values = self._pairs(build_dashboard_values(
            model_version="c6-research-v2", snapshot_as_of="2026-08-28",
            data_status="partial_rankings_only_no_whole_share_replay", slots=[],
            historical_benchmark={"statistical_median_final_nav": 51_306_948.89, "lower_median_actual_route_id": "R38_2023-03-09"},
        ))
        self.assertEqual(values["下次預定提領日"], "2026-09-09")
        self.assertIn("暫不提供", values["目前預估"])
        self.assertEqual(values["期末資產中位數"], 51_306_948.89)

    def test_partial_known_segment_does_not_estimate_withdrawal_from_stale_marks(self):
        values = self._pairs(build_dashboard_values(
            model_version="c6-known", snapshot_as_of="2026-08-12",
            data_status="partial_known_segment_whole_share_replay_pit_blocked_after_20260812",
            slots=[{"slot_id": 1, "ticker": "3653", "shares": 10, "raw_close": 100, "position_cost": 900}],
        ))
        self.assertIn("暫不提供", values["目前預估"])

    def test_dashboard_row_32_contains_current_score0_research_logic(self):
        rows = build_dashboard_values(
            model_version="c6-research-score0-pit-v2-forward-known-segment",
            snapshot_as_of="2026-09-03",
            data_status="ready",
            slots=[],
        )
        self.assertEqual(rows[31], [MODEL_LOGIC] + [""] * 7)
        self.assertIn("C6是研究版，不取代正式V4-D", MODEL_LOGIC)
        self.assertIn("Bottom Score至少60分", MODEL_LOGIC)
        self.assertIn("Launch Score至少65分", MODEL_LOGIC)
        self.assertIn("最高報酬曾達+20%後", MODEL_LOGIC)
        self.assertNotIn("KD風險重排", MODEL_LOGIC)

    def test_dashboard_cash_is_currency_text_not_a_sheet_date_serial(self):
        values = self._pairs(build_dashboard_values(
            model_version="c6-research-v1", snapshot_as_of="2026-08-12",
            data_status="ready", slots=[], cash=168.29,
        ))
        self.assertEqual(values["現金餘額"], 168.29)

    def test_dashboard_resolves_slot_name_from_signal_history(self):
        rows = build_dashboard_values(
            model_version="c6-v2", snapshot_as_of="2026-09-03", data_status="ready",
            slots=[{"slot_id": 1, "ticker": "3653", "shares": 531, "raw_close": 5615, "position_cost": 2_331_423}],
            snapshot_rows=[{"signal_date": "2026-08-06", "rank": 1, "ticker": "3653", "name": "健策", "score": 94.02}],
        )
        slot = next(row for row in rows if row[0] == 1)
        self.assertEqual(slot[1], "3653 健策｜531股")

    def test_event_coverage_pending_is_plain_chinese(self):
        values = self._pairs(build_dashboard_values(
            model_version="c6-research-v2", snapshot_as_of="2026-09-01",
            data_status="partial_whole_share_replay_event_coverage_pending", slots=[],
        ))
        self.assertIn("公司行動覆蓋待確認", values["資料狀態"])


if __name__ == "__main__":
    unittest.main()
