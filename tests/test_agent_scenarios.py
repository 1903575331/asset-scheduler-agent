"""End-to-end acceptance scenarios M01-M10, driven through ``execute_case``.

Each scenario asserts the reviewer-facing contract (review status, draft status,
suggested slot, task status, confirmations, trace shape) and that nothing is ever
published.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _support import (  # noqa: E402
    DRAFT_STATUSES,
    TASK_STATUSES,
    TOOL_NAMES,
    WorkspaceTestCase,
    active_drafts,
    drafts_for,
    find_published_true,
    last_run,
    make_settings,
    outcome_for,
    run_asset,
    scan_text_for_success_claim,
    tool_sequence,
)
from asset_scheduler.runner import execute_case, normalize_case  # noqa: E402

ASSETS = ["M01", "M02", "M03", "M04", "M05", "M06", "M07", "M08", "M09", "M10"]


class AgentScenarioTests(WorkspaceTestCase):
    # ------------------------------------------------------------------ helpers
    def run_one(self, asset_id, **kwargs):
        state = self.state_subdir(asset_id)
        execution = run_asset(asset_id, state, **kwargs)
        run = last_run(execution)
        outcome = outcome_for(execution, asset_id)
        return execution, run, outcome, state

    def assert_common_contract(self, execution, run, outcome, asset_id):
        self.assertEqual(run.status, "completed")
        self.assertIs(run.published, False)
        self.assertIs(outcome.published, False)
        self.assertEqual(outcome.asset_id, asset_id)
        self.assertIn(outcome.task_status, TASK_STATUSES)
        self.assertIn(outcome.draft_status, DRAFT_STATUSES)
        self.assertEqual(run.model, "offline-policy-mock")

        steps = run.steps
        self.assertTrue(steps)
        self.assertEqual([step.index for step in steps], list(range(1, len(steps) + 1)))
        self.assertEqual(run.limits["steps_used"], len(steps))
        self.assertEqual(run.limits["tool_calls_used"], len(tool_sequence(run)))
        for step in steps:
            self.assertIn(step.action, {"tool", "finish", "invalid", "budget_exhausted"})
            self.assertTrue(step.reason)
            if step.action == "tool":
                self.assertIn(step.tool, TOOL_NAMES)
        self.assertEqual(steps[-1].action, "finish")
        self.assertTrue(steps[-1].next_action.startswith("结束"))

        aggregate = execution.aggregate
        self.assertIs(aggregate["published"], False)
        self.assertIs(aggregate["summary"]["published"], False)
        self.assertEqual(len(aggregate["assets"]), 1)
        self.assertIs(aggregate["assets"][0]["published"], False)
        self.assertEqual(aggregate["summary"]["run_status"], {"completed": 1})
        self.assertEqual(find_published_true(aggregate), [])

    def assert_saved_draft(self, state, outcome, *, date, time, channel, confirmation):
        self.assertEqual(outcome.draft_status, "saved")
        self.assertIsNotNone(outcome.draft_id)
        self.assertTrue(outcome.draft_id.startswith("draft-"))
        self.assertEqual(outcome.suggested_slot, {"date": date, "time": time, "channel": channel})
        records = drafts_for(state, outcome.asset_id)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["draft_id"], outcome.draft_id)
        self.assertEqual(record["status"], "draft")
        self.assertIs(record["published"], False)
        self.assertEqual(record["confirmation_status"], confirmation)
        self.assertEqual((record["date"], record["time"], record["channel"]), (date, time, channel))

    def assert_no_draft(self, state, outcome):
        self.assertEqual(outcome.draft_status, "not_attempted")
        self.assertIsNone(outcome.draft_id)
        self.assertIsNone(outcome.draft_error)
        self.assertEqual(drafts_for(state), [])

    # ---------------------------------------------------------------- scenarios
    def test_m01_preferred_slot_conflict_suggests_1900_and_needs_confirmation(self):
        execution, run, outcome, state = self.run_one("M01")
        self.assert_common_contract(execution, run, outcome, "M01")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "completed_pending_confirmation")
        self.assert_saved_draft(
            state, outcome, date="2026-11-02", time="19:00", channel="抖音", confirmation="pending_confirmation"
        )
        self.assertTrue(any("19:00" in question for question in outcome.pending_confirmations))
        self.assertTrue(any("18:00" in question for question in outcome.pending_confirmations))
        self.assertIn("待用户确认", outcome.status_reason)
        self.assertEqual(
            tool_sequence(run),
            ["get_asset", "check_review_rules", "query_publish_slots", "save_schedule_draft", "get_schedule_draft"],
        )
        record = drafts_for(state, "M01")[0]
        self.assertTrue(any("是否同意改为 19:00" in question for question in record["open_questions"]))

    def test_m02_missing_asset_info_requests_input_without_draft(self):
        execution, run, outcome, state = self.run_one("M02")
        self.assert_common_contract(execution, run, outcome, "M02")
        self.assertEqual(outcome.review_status, "needs_input")
        self.assertEqual(outcome.task_status, "needs_input")
        self.assert_no_draft(state, outcome)
        self.assertIsNone(outcome.suggested_slot)
        self.assertIn("素材信息缺失", outcome.status_reason)
        self.assertTrue(any("请补充素材信息" in question for question in outcome.pending_confirmations))
        self.assertTrue(any("owner" in question for question in outcome.pending_confirmations))
        missing = [
            finding for finding in outcome.risk_reasons if finding.get("rule_id") == "R-MISS-01"
        ]
        self.assertTrue(missing)
        self.assertIn("owner", missing[0]["evidence"]["missing_fields"])
        self.assertIn("duration_sec", missing[0]["evidence"]["missing_fields"])
        self.assertEqual(tool_sequence(run), ["get_asset"])

    def test_m03_blocking_rules_abort_scheduling_without_draft(self):
        execution, run, outcome, state = self.run_one("M03")
        self.assert_common_contract(execution, run, outcome, "M03")
        self.assertEqual(outcome.review_status, "blocked")
        self.assertEqual(outcome.task_status, "blocked")
        self.assert_no_draft(state, outcome)
        risk_ids = {finding.get("rule_id") for finding in outcome.risk_reasons}
        self.assertIn("R-COPY-01", risk_ids)
        self.assertIn("R-SENS-01", risk_ids)
        self.assertTrue(any("R-COPY-01" in question for question in outcome.pending_confirmations))
        self.assertIn("阻断", outcome.status_reason)
        self.assertEqual(tool_sequence(run), ["get_asset", "check_review_rules"])

    def test_m04_normal_free_slot_completes_with_saved_draft(self):
        execution, run, outcome, state = self.run_one("M04")
        self.assert_common_contract(execution, run, outcome, "M04")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "completed")
        self.assert_saved_draft(
            state, outcome, date="2026-11-03", time="10:00", channel="抖音", confirmation="not_required"
        )
        self.assertEqual(outcome.pending_confirmations, [])
        self.assertIn("无需确认", outcome.status_reason)
        self.assertEqual(
            tool_sequence(run),
            ["get_asset", "check_review_rules", "query_publish_slots", "save_schedule_draft", "get_schedule_draft"],
        )

    def test_m05_save_failure_is_reported_honestly(self):
        execution, run, outcome, state = self.run_one("M05")
        self.assert_common_contract(execution, run, outcome, "M05")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "failed")
        self.assertEqual(outcome.draft_status, "failed")
        self.assertIsNone(outcome.draft_id)
        self.assertIsNotNone(outcome.draft_error)
        self.assertEqual(outcome.draft_error["code"], "DRAFT_STORE_UNAVAILABLE")
        self.assertIs(outcome.draft_error["retryable"], False)
        self.assertEqual(outcome.suggested_slot["time"], "11:00")
        self.assertIn("草稿保存失败", outcome.status_reason)
        self.assertIn("DRAFT_STORE_UNAVAILABLE", outcome.status_reason)
        self.assertEqual(scan_text_for_success_claim(outcome.status_reason), [])
        self.assertEqual(scan_text_for_success_claim(str(execution.aggregate)), [])
        self.assertTrue(any("草稿未保存" in question for question in outcome.pending_confirmations))
        self.assertEqual(drafts_for(state), [])
        self.assertEqual(tool_sequence(run), ["get_asset", "check_review_rules", "query_publish_slots", "save_schedule_draft"])
        save_step = run.steps[-2]
        self.assertEqual(save_step.tool, "save_schedule_draft")
        self.assertIs(save_step.result_ok, False)
        self.assertEqual(save_step.error_code, "DRAFT_STORE_UNAVAILABLE")

    def test_m06_fixed_requirement_conflict_is_blocked_without_reschedule(self):
        execution, run, outcome, state = self.run_one("M06")
        self.assert_common_contract(execution, run, outcome, "M06")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "blocked")
        self.assert_no_draft(state, outcome)
        self.assertIsNone(outcome.suggested_slot)
        self.assertIn("硬性发布要求 18:00", outcome.status_reason)
        self.assertTrue(any("硬性要求 18:00" in question for question in outcome.pending_confirmations))
        self.assertEqual(tool_sequence(run), ["get_asset", "check_review_rules", "query_publish_slots"])

    def test_m07_transient_save_failure_is_retried_once_then_needs_confirmation(self):
        execution, run, outcome, state = self.run_one("M07")
        self.assert_common_contract(execution, run, outcome, "M07")
        self.assertEqual(outcome.review_status, "needs_confirmation")
        self.assertEqual(outcome.task_status, "completed_pending_confirmation")
        self.assert_saved_draft(
            state, outcome, date="2026-11-05", time="14:00", channel="抖音", confirmation="pending_confirmation"
        )
        save_steps = [step for step in run.steps if step.action == "tool" and step.tool == "save_schedule_draft"]
        self.assertEqual(len(save_steps), 2)
        self.assertIs(save_steps[0].result_ok, False)
        self.assertEqual(save_steps[0].error_code, "DRAFT_STORE_TIMEOUT")
        self.assertIs(save_steps[1].result_ok, True)
        self.assertTrue(any("限时秒杀" in question for question in outcome.pending_confirmations))
        self.assertEqual(len(active_drafts(state, "M07")), 1)

    def test_m08_open_requirement_auto_picks_earliest_free_slot(self):
        execution, run, outcome, state = self.run_one("M08")
        self.assert_common_contract(execution, run, outcome, "M08")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "completed")
        self.assert_saved_draft(
            state, outcome, date="2026-11-05", time="10:00", channel="视频号", confirmation="not_required"
        )
        self.assertEqual(outcome.pending_confirmations, [])
        self.assertEqual(
            tool_sequence(run),
            ["get_asset", "check_review_rules", "query_publish_slots", "save_schedule_draft", "get_schedule_draft"],
        )

    def test_m09_no_available_slot_blocks_scheduling(self):
        execution, run, outcome, state = self.run_one("M09")
        self.assert_common_contract(execution, run, outcome, "M09")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "blocked")
        self.assert_no_draft(state, outcome)
        self.assertIsNone(outcome.suggested_slot)
        self.assertIn("无可用", outcome.status_reason)
        self.assertTrue(any("当天无可用时段" in question for question in outcome.pending_confirmations))
        self.assertEqual(tool_sequence(run), ["get_asset", "check_review_rules", "query_publish_slots"])

    def test_m10_slot_service_failure_fails_run_without_draft(self):
        execution, run, outcome, state = self.run_one("M10")
        self.assert_common_contract(execution, run, outcome, "M10")
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "failed")
        self.assert_no_draft(state, outcome)
        self.assertIsNone(outcome.suggested_slot)
        self.assertIn("时段查询失败", outcome.status_reason)
        self.assertIn("SLOT_SERVICE_ERROR", outcome.status_reason)
        self.assertTrue(any("时段服务暂不可用" in question for question in outcome.pending_confirmations))
        self.assertEqual(tool_sequence(run), ["get_asset", "check_review_rules", "query_publish_slots"])
        slots_step = run.steps[-2]
        self.assertIs(slots_step.result_ok, False)
        self.assertEqual(slots_step.error_code, "SLOT_SERVICE_ERROR")

    # ------------------------------------------------------------- cross-checks
    def test_all_scenarios_preserve_the_no_publish_boundary(self):
        for asset_id in ASSETS:
            with self.subTest(asset=asset_id):
                state = self.state_subdir("boundary-" + asset_id)
                execution = run_asset(asset_id, state)
                run = last_run(execution)
                for record in drafts_for(state):
                    self.assertIs(record["published"], False)
                    self.assertEqual(record["status"], "draft")
                self.assertIs(run.published, False)
                self.assertEqual(find_published_true(run.to_dict()), [])
                self.assertEqual(find_published_true(execution.aggregate), [])

    def test_pending_mode_discovers_and_schedules_all_assets(self):
        state = self.state_subdir("pending")
        settings = make_settings(state, model_provider="mock")
        case = normalize_case(
            {
                "case_id": "batch-pending",
                "request": {"request_id": "req-pending", "mode": "pending", "limit": 10},
            }
        )
        execution = execute_case(case, settings, state_dir=state)
        runs = [entry["result"] for entry in execution.traces]
        self.assertEqual(len(runs), 11)  # 1 discovery run + 10 asset runs
        discovery = runs[0]
        self.assertEqual(discovery.request.mode, "pending")
        self.assertEqual(discovery.status, "completed")
        self.assertEqual(discovery.discovered_assets, ASSETS[:10])

        aggregate = execution.aggregate
        self.assertEqual(aggregate["summary"]["assets"], 10)
        self.assertEqual(aggregate["summary"]["draft_saved"], 4)
        self.assertEqual(aggregate["summary"]["task_status"]["completed"], 2)
        self.assertEqual(aggregate["summary"]["task_status"]["completed_pending_confirmation"], 2)
        self.assertEqual(aggregate["summary"]["task_status"]["blocked"], 3)
        self.assertEqual(aggregate["summary"]["task_status"]["needs_input"], 1)
        self.assertEqual(aggregate["summary"]["task_status"]["failed"], 2)
        self.assertIs(aggregate["published"], False)
        self.assertEqual(len(active_drafts(state)), 4)
        self.assertEqual(sorted(draft["asset_id"] for draft in active_drafts(state)), ["M01", "M04", "M07", "M08"])
