"""Tool contract, guardrail, idempotency and fault-injection tests.

Covers acceptance points 6, 7, 8, 13 (tool level): the tools are the security
boundary, so every guard is asserted through ``Toolbox.call`` exactly the way
the agent calls it.
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
    drafts_doc,
    drafts_for,
    read_json,
)
from asset_scheduler.store import DataRepository  # noqa: E402
from asset_scheduler.tools import Toolbox  # noqa: E402


class ToolContractTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = DataRepository(DATA_DIR, self.state_dir)
        self.toolbox = Toolbox(self.repo)

    # ------------------------------------------------------------------ helpers
    def call(self, name, args):
        return self.toolbox.call(name, args)

    def assert_tool_error(self, result, code):
        self.assertFalse(result.ok, "期望工具失败，实际成功：%r" % (result.data,))
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, code)
        return result.error

    def save_args(self, asset_id, date, time, channel="抖音", **extra):
        args = {"asset_id": asset_id, "date": date, "time": time, "channel": channel}
        args.update(extra)
        return args

    # --------------------------------------------------------------- read tools
    def test_get_asset_returns_metadata_and_missing_fields(self):
        result = self.call("get_asset", {"asset_id": "M04"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["asset"]["asset_id"], "M04")
        self.assertEqual(result.data["asset"]["title"], "日常 Vlog 20s 竖版")
        self.assertEqual(result.data["missing_fields"], [])

    def test_get_asset_reports_missing_fields_for_incomplete_asset(self):
        result = self.call("get_asset", {"asset_id": "M02"})
        self.assertTrue(result.ok)
        self.assertEqual(
            result.data["missing_fields"],
            ["owner", "duration_sec", "publish_window.date"],
        )

    def test_get_asset_is_a_deep_copy(self):
        result = self.call("get_asset", {"asset_id": "M04"})
        result.data["asset"]["title"] = "被测试篡改"
        self.assertEqual(self.repo.asset("M04")["title"], "日常 Vlog 20s 竖版")
        again = self.call("get_asset", {"asset_id": "M04"})
        self.assertEqual(again.data["asset"]["title"], "日常 Vlog 20s 竖版")

    def test_get_asset_unknown_id_is_rejected(self):
        error = self.assert_tool_error(self.call("get_asset", {"asset_id": "NOPE"}), "ASSET_NOT_FOUND")
        self.assertEqual(error.details["asset_id"], "NOPE")

    def test_list_pending_assets_respects_limit(self):
        result = self.call("list_pending_assets", {"limit": 3})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 3)
        rows = result.data["assets"]
        self.assertEqual([row["asset_id"] for row in rows], ["M01", "M02", "M03"])
        for row in rows:
            self.assertIn("publish_window", row)
            self.assertIn("owner", row)

    def test_check_review_rules_blocks_copyright_and_warns_sensitive_words(self):
        result = self.call("check_review_rules", {"asset_id": "M03"})
        self.assertTrue(result.ok)
        self.assertTrue(result.data["blocked"])
        self.assertTrue(result.data["needs_confirmation"])
        by_rule = {finding["rule_id"]: finding for finding in result.data["findings"]}
        self.assertEqual(by_rule["R-COPY-01"]["severity"], "blocking")
        self.assertEqual(by_rule["R-SENS-01"]["severity"], "warn")
        self.assertEqual(by_rule["R-SENS-01"]["evidence"]["matched_words"], ["全网最便宜"])
        self.assertNotIn("R-MUSIC-01", by_rule)

    def test_check_review_rules_clean_asset_has_no_findings(self):
        result = self.call("check_review_rules", {"asset_id": "M06"})
        self.assertTrue(result.ok)
        self.assertFalse(result.data["blocked"])
        self.assertFalse(result.data["needs_confirmation"])
        self.assertEqual(result.data["findings"], [])

    def test_query_publish_slots_reports_occupancy(self):
        result = self.call(
            "query_publish_slots", {"date": "2026-11-02", "channel": "抖音", "asset_id": "M01"}
        )
        self.assertTrue(result.ok)
        slots = {slot["time"]: slot for slot in result.data["slots"]}
        self.assertEqual(slots["18:00"]["status"], "occupied")
        self.assertEqual(slots["18:00"]["occupied_by"]["asset_id"], "M99")
        self.assertEqual(slots["19:00"]["status"], "free")
        self.assertIn("19:00", result.data["available"])
        self.assertNotIn("18:00", result.data["available"])

    def test_query_publish_slots_reports_fully_booked_day(self):
        result = self.call(
            "query_publish_slots", {"date": "2026-11-06", "channel": "抖音", "asset_id": "M09"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["available"], [])
        self.assertTrue(all(slot["status"] == "occupied" for slot in result.data["slots"]))

    def test_query_publish_slots_requires_asset_id_date_and_channel(self):
        # asset_id is mandatory: it keys the fault injection and excludes the
        # asset's own draft from the occupancy check.
        self.assert_tool_error(self.call("query_publish_slots", {"date": "2026-11-02", "channel": "抖音"}), "INVALID_ARGS")
        self.assert_tool_error(self.call("query_publish_slots", {"channel": "抖音", "asset_id": "M01"}), "INVALID_ARGS")
        self.assert_tool_error(self.call("query_publish_slots", {"date": "2026-11-02", "asset_id": "M01"}), "INVALID_ARGS")
        self.assert_tool_error(self.call("query_publish_slots", {}), "INVALID_ARGS")
        self.assert_tool_error(
            self.call("query_publish_slots", {"date": "2026-11-02", "channel": "抖音", "asset_id": ""}),
            "INVALID_ARGS",
        )

    def test_query_publish_slots_fault_injection(self):
        failed = self.call(
            "query_publish_slots",
            {"date": "2026-11-02", "channel": "抖音", "asset_id": "M10"},
        )
        error = self.assert_tool_error(failed, "SLOT_SERVICE_ERROR")
        self.assertFalse(error.retryable)
        # Omitting asset_id can never bypass the injected fault: it is required.
        omitted = self.call("query_publish_slots", {"date": "2026-11-02", "channel": "抖音"})
        self.assert_tool_error(omitted, "INVALID_ARGS")
        # A healthy asset still gets a normal answer.
        healthy = self.call(
            "query_publish_slots", {"date": "2026-11-02", "channel": "抖音", "asset_id": "M01"}
        )
        self.assertTrue(healthy.ok)

    def test_slot_fault_persists_across_repository_reload(self):
        args = {"date": "2026-11-02", "channel": "抖音", "asset_id": "M10"}
        self.assert_tool_error(self.call("query_publish_slots", args), "SLOT_SERVICE_ERROR")
        reloaded = Toolbox(DataRepository(DATA_DIR, self.state_dir))
        self.assert_tool_error(reloaded.call("query_publish_slots", args), "SLOT_SERVICE_ERROR")

    # --------------------------------------------------------------- save tool
    def test_save_draft_persists_a_draft_only(self):
        result = self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "10:00"))
        self.assertTrue(result.ok)
        draft = result.data
        self.assertTrue(draft["draft_id"].startswith("draft-"))
        self.assertEqual(draft["status"], "draft")
        self.assertIs(draft["published"], False)
        self.assertIs(draft["saved"], True)
        self.assertIs(draft["deduplicated"], False)
        self.assertEqual(draft["confirmation_status"], "not_required")
        self.assertEqual((draft["date"], draft["time"], draft["channel"]), ("2026-11-03", "10:00", "抖音"))
        self.assertEqual(draft["decision_note"], "")
        self.assertEqual(draft["supersedes"], [])

        on_disk = read_json(self.state_dir / "drafts.json")
        self.assertIn(draft["draft_id"], on_disk["drafts"])
        record = on_disk["drafts"][draft["draft_id"]]
        self.assertIs(record["published"], False)
        self.assertEqual(record["status"], "draft")
        self.assertNotIn('"published": true', (self.state_dir / "drafts.json").read_text(encoding="utf-8"))

    def test_save_draft_can_be_read_back(self):
        result = self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "10:00"))
        fetched = self.call("get_schedule_draft", {"draft_id": result.data["draft_id"]})
        self.assertTrue(fetched.ok)
        self.assertEqual(fetched.data["draft_id"], result.data["draft_id"])
        self.assertEqual(fetched.data["status"], "draft")
        self.assertIs(fetched.data["published"], False)

    def test_get_schedule_draft_unknown_id_is_rejected(self):
        self.assert_tool_error(self.call("get_schedule_draft", {"draft_id": "draft-nope"}), "DRAFT_NOT_FOUND")

    def test_save_draft_is_idempotent(self):
        first = self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "10:00"))
        second = self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "10:00"))
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(first.data["draft_id"], second.data["draft_id"])
        self.assertIs(first.data["deduplicated"], False)
        self.assertIs(second.data["deduplicated"], True)
        self.assertIs(second.data["saved"], True)
        self.assertEqual(len(active_drafts(self.state_dir, "M04")), 1)
        self.assertEqual(len(drafts_for(self.state_dir, "M04")), 1)

        reloaded = Toolbox(DataRepository(DATA_DIR, self.state_dir))
        fetched = reloaded.call("get_schedule_draft", {"draft_id": first.data["draft_id"]})
        self.assertTrue(fetched.ok)

    def test_save_draft_rejects_incomplete_asset(self):
        error = self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M02", "2026-11-02", "10:00")),
            "INCOMPLETE_ASSET",
        )
        self.assertEqual(
            error.details["missing_fields"],
            ["owner", "duration_sec", "publish_window.date"],
        )
        self.assertEqual(drafts_for(self.state_dir, "M02"), [])

    def test_save_draft_rejects_blocking_review_finding(self):
        error = self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M03", "2026-11-02", "16:00")),
            "RULE_BLOCKING",
        )
        blocking_ids = [finding["rule_id"] for finding in error.details["blocking_findings"]]
        self.assertIn("R-COPY-01", blocking_ids)
        self.assertEqual(drafts_for(self.state_dir, "M03"), [])

    def test_save_draft_rejects_fixed_requirement_change(self):
        error = self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M06", "2026-11-04", "19:00")),
            "HARD_REQUIREMENT_CHANGE_BLOCKED",
        )
        self.assertEqual(error.details["requested_time"], "18:00")
        self.assertEqual(error.details["proposed_time"], "19:00")
        self.assertEqual(active_drafts(self.state_dir), [])

    def test_save_draft_rejects_occupied_fixed_slot(self):
        error = self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M06", "2026-11-04", "18:00")),
            "SLOT_CONFLICT",
        )
        self.assertEqual(error.details["occupied_by"]["asset_id"], "M98")

    def test_save_draft_rejects_channel_change(self):
        # publish_requirement.channel is a hard requirement: M04 is a 抖音 asset,
        # so a model may not silently save the draft on 视频号 instead.
        error = self.assert_tool_error(
            self.call(
                "save_schedule_draft",
                self.save_args("M04", "2026-11-03", "10:00", channel="视频号"),
            ),
            "CHANNEL_NOT_ALLOWED",
        )
        self.assertEqual(error.details["requested_channel"], "抖音")
        self.assertEqual(error.details["proposed_channel"], "视频号")
        self.assertFalse(error.retryable)
        self.assertEqual(drafts_for(self.state_dir, "M04"), [])
        self.assertEqual(active_drafts(self.state_dir), [])
        # The same slot on the required channel still saves normally.
        self.assertTrue(self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "10:00")).ok)
        self.assertEqual(len(active_drafts(self.state_dir, "M04")), 1)

    def test_save_draft_rejects_date_outside_publish_window(self):
        error = self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M04", "2026-11-04", "10:00")),
            "DATE_OUT_OF_WINDOW",
        )
        self.assertEqual(error.details["publish_window"], "2026-11-03")

    def test_save_draft_rejects_occupied_slot(self):
        error = self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "12:00")),
            "SLOT_CONFLICT",
        )
        self.assertEqual(error.details["occupied_by"]["asset_id"], "M97")
        self.assertEqual(active_drafts(self.state_dir), [])

    def test_save_draft_rejects_invalid_time_argument(self):
        self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "25:00")),
            "INVALID_ARGS",
        )
        self.assert_tool_error(
            self.call("save_schedule_draft", self.save_args("M04", "2026-11-03", "09:00")),
            "INVALID_ARGS",
        )
        self.assert_tool_error(
            self.call("save_schedule_draft", {"asset_id": "M04", "date": "2026-11-03", "channel": "抖音"}),
            "INVALID_ARGS",
        )

    def test_save_draft_rejects_empty_optional_values_but_keeps_note_optional(self):
        result = self.call(
            "save_schedule_draft",
            {
                "asset_id": "M04",
                "date": "2026-11-03",
                "time": "10:00",
                "channel": "抖音",
                "decision_note": "测试备注",
                "open_questions": [],
            },
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["decision_note"], "测试备注")
        self.assertEqual(result.data["open_questions"], [])

    def test_preferred_change_is_marked_pending_confirmation(self):
        result = self.call("save_schedule_draft", self.save_args("M01", "2026-11-02", "19:00"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["confirmation_status"], "pending_confirmation")
        self.assertTrue(any("18:00" in question and "19:00" in question for question in result.data["open_questions"]))
        self.assertIs(result.data["published"], False)

    def test_transient_save_failure_is_retryable_and_then_succeeds(self):
        first = self.call("save_schedule_draft", self.save_args("M07", "2026-11-05", "14:00"))
        error = self.assert_tool_error(first, "DRAFT_STORE_TIMEOUT")
        self.assertTrue(error.retryable)
        self.assertEqual(drafts_for(self.state_dir, "M07"), [])

        second = self.call("save_schedule_draft", self.save_args("M07", "2026-11-05", "14:00"))
        self.assertTrue(second.ok)
        self.assertIs(second.data["deduplicated"], False)
        self.assertEqual(len(active_drafts(self.state_dir, "M07")), 1)
        self.assertEqual(second.data["confirmation_status"], "pending_confirmation")
        self.assertTrue(any("限时秒杀" in question for question in second.data["open_questions"]))

    def test_permanent_save_failure_never_writes_a_draft(self):
        for _ in range(2):
            error = self.assert_tool_error(
                self.call("save_schedule_draft", self.save_args("M05", "2026-11-03", "11:00")),
                "DRAFT_STORE_UNAVAILABLE",
            )
            self.assertFalse(error.retryable)
        self.assertEqual(drafts_for(self.state_dir, "M05"), [])
        self.assertEqual(active_drafts(self.state_dir), [])
        doc = drafts_doc(self.state_dir)
        self.assertEqual(doc["drafts"], {})

    # --------------------------------------------------------- guardrail tests
    def test_unknown_tool_is_rejected(self):
        error = self.assert_tool_error(
            self.call("publish_asset", {"asset_id": "M04"}),
            "UNKNOWN_TOOL",
        )
        self.assertFalse(error.retryable)

    def test_non_dict_arguments_are_rejected(self):
        self.assert_tool_error(self.call("get_asset", ["M04"]), "INVALID_ARGS")
        self.assert_tool_error(self.call("save_schedule_draft", "M04"), "INVALID_ARGS")

    def test_no_publish_tool_is_exposed(self):
        self.assertEqual(
            set(TOOL_NAMES),
            {
                "list_pending_assets",
                "get_asset",
                "check_review_rules",
                "query_publish_slots",
                "save_schedule_draft",
                "get_schedule_draft",
            },
        )
        for spec in Toolbox.specs():
            name = spec["name"].lower()
            self.assertFalse(
                name.startswith(("publish_", "release_", "ship_")),
                "发现疑似发布工具：%s" % name,
            )
            self.assertNotIn(name, {"publish", "release", "ship", "post"})
        save_spec = {spec["name"]: spec for spec in Toolbox.specs()}["save_schedule_draft"]
        self.assertIn("仅草稿", save_spec["description"])
        self.assertIn("永不发布", save_spec["description"])
        for forbidden in ("publish_asset", "publish_schedule", "release_schedule", "ship_asset"):
            self.assert_tool_error(self.call(forbidden, {}), "UNKNOWN_TOOL")

    def test_tool_specs_carry_required_parameters(self):
        specs = {spec["name"]: spec for spec in Toolbox.specs()}
        self.assertEqual(specs["save_schedule_draft"]["parameters"]["required"], ["asset_id", "date", "time", "channel"])
        self.assertEqual(specs["get_asset"]["parameters"]["required"], ["asset_id"])
        self.assertEqual(specs["query_publish_slots"]["parameters"]["required"], ["asset_id", "date", "channel"])
        for spec in specs.values():
            self.assertEqual(spec["parameters"]["type"], "object")
            self.assertTrue(spec["description"])
