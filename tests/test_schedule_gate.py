from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime, time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rotation_radar.schedule_gate import (
    ScheduleGateRules,
    add_recent_emergency_market_closure,
    evaluate_schedule_gate,
    fetch_twse_calendar,
    is_trading_day,
    load_schedule_rules,
)


class ScheduleGateTests(unittest.TestCase):
    def test_emergency_friday_closure_does_not_backfill_thursday(self) -> None:
        now = datetime(2026, 7, 10, 18, 0)
        rules = ScheduleGateRules(run_after=time(15, 0))

        with patch("rotation_radar.schedule_gate.fetch_official_market_session_state", return_value="closed"):
            closed = add_recent_emergency_market_closure(now, set(), set(), rules)
        decision = evaluate_schedule_gate(now, set(), closed, rules=rules)

        self.assertIn(date(2026, 7, 10), closed)
        self.assertFalse(decision.should_run)
        self.assertIsNone(decision.target_date)

    def test_unknown_session_state_does_not_invent_closure(self) -> None:
        now = datetime(2026, 7, 10, 18, 0)
        rules = ScheduleGateRules(run_after=time(15, 0))

        with patch("rotation_radar.schedule_gate.fetch_official_market_session_state", return_value="unknown"):
            closed = add_recent_emergency_market_closure(now, set(), set(), rules)

        self.assertNotIn(date(2026, 7, 10), closed)

    def test_daily_gate_runs_only_current_trading_day_after_window_opens(self) -> None:
        before_15 = evaluate_schedule_gate(datetime(2026, 6, 5, 14, 59), set(), set())
        after_15 = evaluate_schedule_gate(datetime(2026, 6, 5, 15, 0), set(), set())
        weekend = evaluate_schedule_gate(datetime(2026, 6, 6, 15, 0), set(), set())

        self.assertFalse(before_15.should_run)
        self.assertIsNone(before_15.target_date)
        self.assertTrue(after_15.should_run)
        self.assertEqual(after_15.target_date, date(2026, 6, 5))
        self.assertEqual(after_15.reason, "trading_day_after_run_after_taipei")
        self.assertFalse(weekend.should_run)
        self.assertIsNone(weekend.target_date)

    def test_daily_gate_skips_when_today_is_closed(self) -> None:
        closed_dates = {date(2026, 6, 5)}

        closed = evaluate_schedule_gate(datetime(2026, 6, 5, 15, 0), set(), closed_dates)

        self.assertFalse(closed.should_run)
        self.assertIsNone(closed.target_date)
        self.assertEqual(closed.reason, "not_trading_day")

    def test_daily_gate_skips_before_first_open_window(self) -> None:
        closed_dates = {
            date(2026, 5, 18),
            date(2026, 5, 19),
            date(2026, 5, 20),
            date(2026, 5, 21),
            date(2026, 5, 22),
            date(2026, 5, 25),
            date(2026, 5, 26),
            date(2026, 5, 27),
            date(2026, 5, 28),
            date(2026, 5, 29),
        }

        decision = evaluate_schedule_gate(datetime(2026, 6, 1, 14, 59), set(), closed_dates)

        self.assertFalse(decision.should_run)
        self.assertEqual(decision.reason, "before_run_after_taipei")

    def test_daily_gate_uses_shared_run_after_rule(self) -> None:
        rules = ScheduleGateRules(run_after=datetime.strptime("16:00", "%H:%M").time())
        before_shared_time = evaluate_schedule_gate(datetime(2026, 6, 5, 15, 30), set(), set(), rules=rules)
        after_shared_time = evaluate_schedule_gate(datetime(2026, 6, 5, 16, 0), set(), set(), rules=rules)

        self.assertIsNone(before_shared_time.target_date)
        self.assertEqual(before_shared_time.reason, "before_run_after_taipei")
        self.assertEqual(after_shared_time.target_date, date(2026, 6, 5))
        self.assertEqual(after_shared_time.reason, "trading_day_after_run_after_taipei")

    def test_load_schedule_rules_reads_shared_daily_profile(self) -> None:
        rules_file = Path("tmp_schedule_rules_test.json")
        rules_file.write_text(
            '{"profiles":{"daily":{"run_after":"16:30","retry_until_success":false}}}',
            encoding="utf-8",
        )
        try:
            rules = load_schedule_rules(rules_file)
        finally:
            rules_file.unlink()

        self.assertEqual(rules.run_after, datetime.strptime("16:30", "%H:%M").time())
        self.assertFalse(rules.retry_until_success)

    def test_daily_gate_respects_retry_until_success_false(self) -> None:
        rules = ScheduleGateRules(retry_until_success=False)
        decision = evaluate_schedule_gate(datetime(2026, 6, 6, 15, 0), set(), set(), rules=rules)

        self.assertFalse(decision.should_run)
        self.assertEqual(decision.reason, "not_trading_day")

    def test_open_dates_override_weekend_rule(self) -> None:
        make_up_trading_day = date(2026, 6, 6)

        self.assertTrue(is_trading_day(make_up_trading_day, {make_up_trading_day}, set()))

    def test_fetch_twse_calendar_falls_back_when_api_returns_invalid_json(self) -> None:
        with patch("rotation_radar.schedule_gate.urlopen", return_value=_response("not json")):
            open_dates, closed_dates = fetch_twse_calendar()

        self.assertEqual(open_dates, set())
        self.assertIsNone(closed_dates)

    def test_fetch_twse_calendar_falls_back_when_payload_has_no_closed_dates(self) -> None:
        with patch("rotation_radar.schedule_gate.urlopen", return_value=_response("[]")):
            open_dates, closed_dates = fetch_twse_calendar()

        self.assertEqual(open_dates, set())
        self.assertIsNone(closed_dates)

    def test_daily_gate_skips_without_failure_when_calendar_is_unavailable(self) -> None:
        decision = evaluate_schedule_gate(datetime(2026, 6, 5, 15, 0), set(), None)

        self.assertFalse(decision.should_run)
        self.assertEqual(decision.reason, "calendar_unavailable")

    def test_workflow_manual_report_date_override_sets_should_run(self) -> None:
        workflow = Path(".github/workflows/generate-report.yml").read_text(encoding="utf-8")

        self.assertIn('cron: "0 7-15 * * 1-5"', workflow)
        self.assertNotIn('cron: "0 * * * *"', workflow)
        self.assertIn("manual workflow_dispatch; fallback allowed unless exact mode is enabled", workflow)
        self.assertIn("--manual-rerun", workflow)
        self.assertIn("--require-exact-report-date", workflow)
        self.assertIn("actual_report_date", workflow)
        self.assertIn("echo \"should_run=true\"", workflow)
        self.assertIn("if: steps.report-date.outputs.should_run == 'true'", workflow)
        self.assertIn("Checkout shared schedule rules", workflow)
        self.assertIn("SCHEDULE_RULES_PATH:", workflow)
        self.assertIn("Stop scheduled retry after successful trading-date publish", workflow)
        self.assertIn("Check fixed Google Drive report completion", workflow)
        self.assertIn("--check-public-report", workflow)
        self.assertIn("pip install '.[test]'", workflow)
        self.assertIn("steps.drive-completion.outputs.already_published != 'true'", workflow)
        self.assertIn("github.event_name == 'schedule' && steps.daily-publish-marker.outputs.cache-hit == 'true'", workflow)
        self.assertIn("github.event_name == 'schedule' && steps.daily-publish-marker.outputs.cache-hit != 'true'", workflow)
        self.assertIn('if [ "$status" -eq 75 ]', workflow)
        self.assertIn('echo "report_ready=false"', workflow)
        self.assertIn("if: steps.generate-report.outputs.report_ready == 'true'", workflow)
        self.assertIn("Validate formal signal checkpoint", workflow)
        self.assertIn("validate_formal_signal_checkpoint(checkpoint)", workflow)
        self.assertIn("data/formal_0050_00631l_state.json", workflow)
        self.assertIn('git pull --rebase --autostash origin "${branch}"', workflow)
        self.assertIn('git push origin "HEAD:${branch}"', workflow)
        self.assertIn("for attempt in 1 2 3", workflow)
        self.assertNotIn(
            'checkpoint["actual_trade_date"] != checkpoint["model_next_day_execution_date"]',
            workflow,
        )

    def test_manual_v4d_report_exposes_incomplete_data_to_batch(self) -> None:
        workflow = Path(
            ".github/workflows/v4d-dashboard-worker.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('if [ "${{ github.event_name }}" = "workflow_dispatch" ]', workflow)
        self.assertIn("::notice::", workflow)
        self.assertIn("exit 75", workflow)
        self.assertIn("--recover-delayed-schedule", workflow)

    def test_delayed_schedule_can_recover_previous_trading_day(self) -> None:
        rules = ScheduleGateRules(run_after=time(15, 0), retry_until_success=True)
        decision = evaluate_schedule_gate(
            datetime(2026, 8, 29, 3, 8),
            set(),
            set(),
            rules=rules,
        )

        self.assertTrue(decision.should_run)
        self.assertEqual(decision.target_date, date(2026, 8, 28))
        self.assertEqual(decision.reason, "retry_previous_trading_day_until_published")

    def test_manual_workflows_recover_latest_completed_session_before_close(self) -> None:
        for workflow_path in (
            Path(".github/workflows/generate-report.yml"),
            Path(".github/workflows/v4d-dashboard-worker.yml"),
        ):
            workflow = workflow_path.read_text(encoding="utf-8")
            self.assertIn(
                "python -m rotation_radar.schedule_gate --recover-delayed-schedule",
                workflow,
            )

    def test_v4d_data_not_ready_cannot_report_batch_success(self) -> None:
        workflow = Path(
            ".github/workflows/v4d-dashboard-worker.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("exit 75"), 2)


@contextmanager
def _response(text: str):
    yield StringIO(text)


if __name__ == "__main__":
    unittest.main()
    add_recent_emergency_market_closure,
