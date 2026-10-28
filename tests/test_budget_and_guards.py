"""Execution-budget, no-publish and model-trust guardrail tests (points 10, 13).

The runtime derives every outcome from tool observations; these tests prove that
a model cannot claim a published/saved state and that budget exhaustion yields an
explicit ``incomplete`` result instead of a silent success.
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
    DATA_DIR,
    TOOL_NAMES,
    WorkspaceTestCase,
    active_drafts,
    drafts_for,
    fake_model,
    find_published_true,
    last_run,
    outcome_for,
    run_asset,
    single_asset_request,
)
from asset_scheduler.agent import ReActAgent  # noqa: E402
from asset_scheduler.domain import Decision  # noqa: E402
from asset_scheduler.llm import ModelError  # noqa: E402
from asset_scheduler.store import DataRepository  # noqa: E402
from asset_scheduler.tools import Toolbox  # noqa: E402


class BudgetAndGuardTests(WorkspaceTestCase):
    # ------------------------------------------------------------------ helpers
    def _run_agent(self, decisions, *, state_name="agent", max_steps=14, max_tool_calls=16):
        state = self.state_subdir(state_name)
        repo = DataRepository(DATA_DIR, state)
        agent = ReActAgent(
            model=fake_model(decisions),
            toolbox=Toolbox(repo),
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
        )
        result = agent.run(single_asset_request("M04"))
        return result, repo, state

    def assert_run_incomplete(self, execution, asset_id):
        run = last_run(execution)
        outcome = outcome_for(execution, asset_id)
        self.assertEqual(run.status, "incomplete")
        self.assertIs(run.published, False)
        self.assertEqual(outcome.task_status, "incomplete")
        self.assertEqual(outcome.draft_status, "not_attempted")
        self.assertIsNone(outcome.draft_id)
        self.assertIs(outcome.published, False)
        return run, outcome

    # -------------------------------------------------------------- step budget
    def test_max_steps_limit_marks_run_and_asset_incomplete(self):
        state = self.state_subdir("steps")
        execution = run_asset("M01", state, max_steps=2)
        run, outcome = self.assert_run_incomplete(execution, "M01")
        self.assertIn("达到步数上限 2", run.status_reason)
        self.assertIn("步数上限", outcome.status_reason)
        self.assertEqual(run.limits["max_steps"], 2)
        self.assertEqual(run.limits["steps_used"], 2)
        self.assertEqual(run.limits["tool_calls_used"], 2)
        self.assertEqual([step.action for step in run.steps], ["tool", "tool"])
        self.assertEqual(execution.aggregate["summary"]["run_status"], {"incomplete": 1})
        self.assertEqual(execution.aggregate["assets"][0]["task_status"], "incomplete")
        self.assertEqual(execution.aggregate["summary"]["draft_saved"], 0)
        self.assertEqual(drafts_for(state), [])

    def test_run_options_can_override_max_steps(self):
        state = self.state_subdir("steps-option")
        execution = run_asset("M01", state, max_steps=14, run_options={"max_steps": 2})
        run, _ = self.assert_run_incomplete(execution, "M01")
        self.assertEqual(run.limits["max_steps"], 2)
        self.assertEqual(run.limits["steps_used"], 2)
        self.assertIn("达到步数上限 2", run.status_reason)

    # ---------------------------------------------------------- tool call budget
    def test_max_tool_calls_stops_before_dispatching_extra_tool(self):
        state = self.state_subdir("tools")
        execution = run_asset("M01", state, max_tool_calls=1)
        run, outcome = self.assert_run_incomplete(execution, "M01")
        self.assertIn("达到工具调用上限 1", run.status_reason)
        self.assertIn("工具调用上限", outcome.status_reason)
        self.assertEqual(run.limits["max_tool_calls"], 1)
        self.assertEqual(run.limits["tool_calls_used"], 1)
        self.assertEqual(run.limits["steps_used"], 2)
        last_step = run.steps[-1]
        self.assertEqual(last_step.action, "budget_exhausted")
        self.assertEqual(last_step.tool, "check_review_rules")
        self.assertEqual(execution.aggregate["summary"]["run_status"], {"incomplete": 1})
        self.assertEqual(drafts_for(state), [])

    def test_run_options_can_override_max_tool_calls(self):
        state = self.state_subdir("tools-option")
        execution = run_asset("M01", state, max_tool_calls=16, run_options={"max_tool_calls": 1})
        run, _ = self.assert_run_incomplete(execution, "M01")
        self.assertEqual(run.limits["max_tool_calls"], 1)
        self.assertEqual(run.steps[-1].action, "budget_exhausted")

    def test_zero_tool_call_budget_fails_before_first_call(self):
        state = self.state_subdir("tools-zero")
        execution = run_asset("M01", state, max_tool_calls=0)
        run, _ = self.assert_run_incomplete(execution, "M01")
        self.assertEqual(run.limits["tool_calls_used"], 0)
        self.assertEqual(run.limits["steps_used"], 1)
        step = run.steps[0]
        self.assertEqual(step.action, "budget_exhausted")
        self.assertEqual(step.tool, "get_asset")
        self.assertEqual(drafts_for(state), [])

    def test_agent_clamps_invalid_budgets(self):
        repo = DataRepository(DATA_DIR, self.state_subdir("clamp"))
        agent = ReActAgent(fake_model([]), Toolbox(repo), max_steps=0, max_tool_calls=-3)
        self.assertEqual(agent.max_steps, 1)
        self.assertEqual(agent.max_tool_calls, 0)

    # ------------------------------------------------------------- no publishing
    def test_no_publish_tool_is_exposed_to_the_model(self):
        self.assertEqual(len(TOOL_NAMES), 6)
        for name in TOOL_NAMES:
            lower = name.lower()
            self.assertFalse(
                lower.startswith(("publish_", "release_", "ship_")),
                "发现疑似发布工具：%s" % name,
            )
            self.assertNotIn(lower, {"publish", "release", "ship", "post"})
        repo = DataRepository(DATA_DIR, self.state_subdir("publish-tool"))
        toolbox = Toolbox(repo)
        for forbidden in ("publish_asset", "publish_schedule_draft", "release_schedule"):
            result = toolbox.call(forbidden, {"asset_id": "M04"})
            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, "UNKNOWN_TOOL")

    def test_results_are_never_marked_as_published(self):
        state = self.state_subdir("published-flag")
        execution = run_asset("M04", state)
        run = last_run(execution)
        outcome = outcome_for(execution, "M04")
        self.assertIs(run.published, False)
        self.assertIs(outcome.published, False)
        self.assertIs(execution.aggregate["published"], False)
        self.assertIs(execution.aggregate["summary"]["published"], False)
        self.assertEqual(find_published_true(execution.aggregate), [])
        self.assertEqual(find_published_true(run.to_dict()), [])
        for record in drafts_for(state):
            self.assertIs(record["published"], False)
        self.assertNotIn('"published": true', str(execution.aggregate))

    def test_model_cannot_declare_success_and_publish_by_finishing(self):
        result, repo, state = self._run_agent([Decision(action="finish", reason="已发布成功！")])
        self.assertEqual(result.status, "completed")
        outcome = result.assets[0]
        self.assertIn(outcome.task_status, {"failed", "incomplete"})
        self.assertIs(outcome.published, False)
        self.assertEqual(find_published_true(result.to_dict()), [])
        self.assertEqual(active_drafts(state), [])
        self.assertEqual(repo.drafts_for_asset("M04"), [])

    def test_model_calling_a_nonexistent_publish_tool_cannot_mutate_state(self):
        result, repo, state = self._run_agent(
            [
                Decision(action="tool", reason="直接发布", tool="publish_asset", args={"asset_id": "M04"}),
                Decision(action="finish", reason="结束。"),
            ]
        )
        self.assertEqual(result.status, "completed")
        first_step = result.steps[0]
        self.assertEqual(first_step.action, "tool")
        self.assertEqual(first_step.error_code, "UNKNOWN_TOOL")
        self.assertIs(first_step.result_ok, False)
        outcome = result.assets[0]
        self.assertIsNotNone(outcome)
        self.assertIs(outcome.published, False)
        self.assertEqual(outcome.draft_status, "not_attempted")
        self.assertEqual(repo.drafts_for_asset("M04"), [])
        # Published files are never produced; drafts store stays empty.
        self.assertEqual(active_drafts(state), [])

    def test_model_cannot_change_the_publish_channel(self):
        result, repo, state = self._run_agent(
            [
                Decision(action="tool", reason="读取素材", tool="get_asset", args={"asset_id": "M04"}),
                Decision(
                    action="tool",
                    reason="检查规则",
                    tool="check_review_rules",
                    args={"asset_id": "M04"},
                ),
                Decision(
                    action="tool",
                    reason="查询时段",
                    tool="query_publish_slots",
                    args={"date": "2026-11-03", "channel": "抖音", "asset_id": "M04"},
                ),
                Decision(
                    action="tool",
                    reason="改到视频号保存",
                    tool="save_schedule_draft",
                    args={
                        "asset_id": "M04",
                        "date": "2026-11-03",
                        "time": "10:00",
                        "channel": "视频号",
                    },
                ),
                Decision(action="finish", reason="保存被拒绝，需用户决定。"),
            ]
        )
        self.assertEqual(result.status, "completed")
        save_step = [step for step in result.steps if step.tool == "save_schedule_draft"][0]
        self.assertIs(save_step.result_ok, False)
        self.assertEqual(save_step.error_code, "CHANNEL_NOT_ALLOWED")

        outcome = result.assets[0]
        self.assertEqual(outcome.review_status, "approved")
        self.assertEqual(outcome.task_status, "blocked")
        self.assertEqual(outcome.draft_status, "failed")
        self.assertEqual(outcome.draft_error["code"], "CHANNEL_NOT_ALLOWED")
        self.assertIn("发布渠道不得擅自修改", outcome.status_reason)
        self.assertTrue(any("CHANNEL_NOT_ALLOWED" in question for question in outcome.pending_confirmations))
        self.assertIs(outcome.published, False)
        self.assertEqual(find_published_true(result.to_dict()), [])
        self.assertEqual(drafts_for(state), [])
        self.assertEqual(repo.drafts_for_asset("M04"), [])
        self.assertEqual(active_drafts(state), [])

    # ------------------------------------------------------------ model failures
    def test_invalid_model_action_fails_the_run(self):
        result, _repo, state = self._run_agent([Decision(action="publish", reason="非法动作")])
        self.assertEqual(result.status, "failed")
        self.assertIn("非法动作", result.status_reason)
        self.assertEqual(result.steps[-1].action, "invalid")
        outcome = result.assets[0]
        self.assertEqual(outcome.task_status, "failed")
        self.assertIs(outcome.published, False)
        self.assertEqual(drafts_for(state), [])

    def test_model_error_fails_the_run(self):
        result, _repo, _state = self._run_agent([ModelError("网络中断（测试）")])
        self.assertEqual(result.status, "failed")
        self.assertIn("模型调用失败", result.status_reason)
        self.assertIn("网络中断", result.status_reason)
        self.assertEqual(result.assets[0].task_status, "failed")
        self.assertIs(result.assets[0].published, False)

    def test_non_decision_model_return_fails_the_run(self):
        result, _repo, _state = self._run_agent([object()])
        self.assertEqual(result.status, "failed")
        self.assertIn("非 Decision", result.status_reason)
        self.assertIs(result.published, False)
