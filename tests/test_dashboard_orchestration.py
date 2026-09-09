import unittest
from pathlib import Path
from datetime import date, datetime, time
from rotation_radar.schedule_gate import ScheduleGateRules, evaluate_schedule_gate

ROOT = Path(__file__).resolve().parents[1]

class DashboardOrchestrationTests(unittest.TestCase):
    def test_single_schedule_and_parallel_children(self):
        parent = (ROOT / '.github/workflows/publish-all-stock-dashboards.yml').read_text(encoding='utf-8')
        self.assertIn('cron: "15 9 * * 1-5"', parent)
        self.assertEqual(parent.count('needs: target'), 2)
        self.assertIn('needs: [target, v4d, c6]', parent)
        self.assertIn('test "$V4D_RESULT" = success', parent)
        self.assertIn('test "$C6_RESULT" = success', parent)
        for name in ['v4d-dashboard-worker.yml', 'publish-c6-research-dashboard.yml']:
            child = (ROOT / '.github/workflows' / name).read_text(encoding='utf-8')
            self.assertNotIn('cron:', child)
            self.assertIn('workflow_call:', child)
            self.assertIn('inputs.report_date', child)

    def test_common_cutoff_does_not_change_shared_defaults(self):
        self.assertEqual(ScheduleGateRules().run_after, time(15))
        rules = ScheduleGateRules(time(17,15), True)
        before = evaluate_schedule_gate(datetime(2026,9,8,17,14), set(), set(), rules=rules)
        after = evaluate_schedule_gate(datetime(2026,9,8,17,15), set(), set(), rules=rules)
        self.assertEqual(before.target_date, date(2026,9,7))
        self.assertEqual(after.target_date, date(2026,9,8))

    def test_v4d_readiness_failure_cannot_be_green(self):
        child = (ROOT / '.github/workflows/v4d-dashboard-worker.yml').read_text(encoding='utf-8')
        self.assertEqual(child.count('exit 75'), 2)

    def test_legacy_entry_calls_all_without_cycle(self):
        wrapper = (ROOT / '.github/workflows/generate-base-cycle-top10-report.yml').read_text(encoding='utf-8')
        parent = (ROOT / '.github/workflows/publish-all-stock-dashboards.yml').read_text(encoding='utf-8')
        self.assertIn('uses: ./.github/workflows/publish-all-stock-dashboards.yml', wrapper)
        self.assertNotIn('run:', wrapper)
        self.assertIn('workflow_call:', parent)
        self.assertNotIn('uses: ./.github/workflows/generate-base-cycle-top10-report.yml', parent)

if __name__ == '__main__':
    unittest.main()
