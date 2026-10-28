"""Idempotent resubmission and re-plan/supersede tests (acceptance points 6, 11)."""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _support import (  # noqa: E402
    DATA_DIR,
    WorkspaceTestCase,
    active_drafts,
    drafts_doc,
    drafts_for,
    last_run,
    outcome_for,
    read_json,
    run_asset,
    write_json,
)
from asset_scheduler.store import DataRepository  # noqa: E402


class IdempotencyTests(WorkspaceTestCase):
    def test_repeat_submission_reuses_the_existing_draft(self):
        state = self.state_subdir("idem")
        first = run_asset("M04", state)
        first_outcome = outcome_for(first, "M04")
        self.assertEqual(first_outcome.draft_status, "saved")
        first_run = last_run(first)
        self.assertEqual(first_run.status, "completed")

        second = run_asset("M04", state)
        second_outcome = outcome_for(second, "M04")
        second_run = last_run(second)
        self.assertEqual(second_run.status, "completed")
        self.assertEqual(second_outcome.draft_status, "duplicate_existing")
        self.assertEqual(second_outcome.task_status, "completed")
        self.assertEqual(second_outcome.draft_id, first_outcome.draft_id)
        self.assertEqual(second_outcome.suggested_slot, first_outcome.suggested_slot)
        self.assertIs(second_outcome.published, False)
        self.assertIn("幂等", second_outcome.status_reason)
        self.assertIn("相同请求", second_outcome.status_reason)

        records = drafts_for(state, "M04")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(active_drafts(state)), 1)
        self.assertEqual(records[0]["draft_id"], first_outcome.draft_id)
        self.assertIs(records[0]["published"], False)
        self.assertEqual(records[0]["status"], "draft")
        # A duplicate submission counts as a saved draft in the aggregate, but
        # there is still only one physical record.
        self.assertEqual(first.aggregate["summary"]["draft_saved"], 1)
        self.assertEqual(second.aggregate["summary"]["draft_saved"], 1)

    def test_duplicate_submission_trace_mentions_the_idempotent_hit(self):
        state = self.state_subdir("idem-trace")
        run_asset("M04", state)
        second = run_asset("M04", state)
        run = last_run(second)
        save_steps = [step for step in run.steps if step.tool == "save_schedule_draft"]
        self.assertEqual(len(save_steps), 1)
        self.assertIs(save_steps[0].result_ok, True)
        self.assertIn("幂等命中", save_steps[0].result_summary)
        self.assertIn("未重复创建", save_steps[0].result_summary)

    def test_draft_is_visible_after_repository_reload(self):
        state = self.state_subdir("reload")
        execution = run_asset("M04", state)
        outcome = outcome_for(execution, "M04")
        reloaded = DataRepository(DATA_DIR, state)
        draft = reloaded.find_draft(outcome.draft_id)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["status"], "draft")
        self.assertIs(draft["published"], False)

    def test_replan_after_slot_occupied_supersedes_previous_draft(self):
        data_dir = self.copy_data_dir()
        state = self.state_subdir("replan")

        first = run_asset("M01", state, data_dir=data_dir)
        first_outcome = outcome_for(first, "M01")
        self.assertEqual(first_outcome.draft_status, "saved")
        self.assertEqual(first_outcome.suggested_slot["time"], "19:00")
        first_draft_id = first_outcome.draft_id

        # Someone else books the 19:00 slot the first draft used (copied data,
        # the repository's data/ is never modified).
        schedule_path = data_dir / "schedule.json"
        schedule = read_json(schedule_path)
        schedule["bookings"].append(
            {
                "date": "2026-11-02",
                "channel": "抖音",
                "time": "19:00",
                "asset_id": "M77",
                "title": "新增占用：19:00",
            }
        )
        write_json(schedule_path, schedule)

        second = run_asset("M01", state, data_dir=data_dir)
        second_outcome = outcome_for(second, "M01")
        self.assertEqual(second_outcome.draft_status, "saved")
        self.assertEqual(second_outcome.task_status, "completed_pending_confirmation")
        self.assertEqual(second_outcome.suggested_slot["date"], "2026-11-02")
        self.assertEqual(second_outcome.suggested_slot["time"], "20:00")
        self.assertNotEqual(second_outcome.draft_id, first_draft_id)
        self.assertTrue(any("20:00" in question for question in second_outcome.pending_confirmations))

        records = drafts_for(state, "M01")
        self.assertEqual(len(records), 2)
        active = active_drafts(state, "M01")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["draft_id"], second_outcome.draft_id)
        self.assertEqual(active[0]["time"], "20:00")
        self.assertEqual(active[0]["status"], "draft")
        self.assertIs(active[0]["published"], False)

        old = [record for record in records if record["draft_id"] == first_draft_id][0]
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], second_outcome.draft_id)
        self.assertIs(old["published"], False)
        self.assertIn(first_draft_id, active[0]["supersedes"])

        # Only one active draft in the whole store after the replan.
        self.assertEqual(len(active_drafts(state)), 1)
        self.assertEqual(len(drafts_doc(state)["drafts"]), 2)
        self.assertIs(last_run(second).published, False)

    def test_retryable_failure_then_success_creates_only_one_draft(self):
        state = self.state_subdir("retry")
        execution = run_asset("M07", state)
        outcome = outcome_for(execution, "M07")
        self.assertEqual(outcome.draft_status, "saved")
        self.assertEqual(outcome.task_status, "completed_pending_confirmation")
        records = drafts_for(state, "M07")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "draft")
        self.assertEqual(len(active_drafts(state)), 1)
        save_steps = [step for step in last_run(execution).steps if step.tool == "save_schedule_draft"]
        self.assertEqual(len(save_steps), 2)

    def test_repeated_failed_save_never_creates_a_draft(self):
        state = self.state_subdir("perm-fail")
        for _ in range(2):
            execution = run_asset("M05", state)
            outcome = outcome_for(execution, "M05")
            self.assertEqual(outcome.draft_status, "failed")
            self.assertEqual(outcome.task_status, "failed")
            self.assertIs(outcome.published, False)
            self.assertEqual(drafts_for(state, "M05"), [])
            self.assertEqual(active_drafts(state), [])
