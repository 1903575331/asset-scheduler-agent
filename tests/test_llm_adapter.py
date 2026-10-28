"""OpenAI-compatible adapter protocol tests (acceptance point 12).

A loopback-only ``ThreadingHTTPServer`` scripts the ``/chat/completions``
responses, so the real adapter code path (request shape, headers, transcript
conversion, tool-call parsing, error handling) is exercised without any network
access.  The adapters are also driven end-to-end through ``ReActAgent``.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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
    find_published_true,
)
from asset_scheduler.agent import ReActAgent  # noqa: E402
from asset_scheduler.domain import AgentState, Request  # noqa: E402
from asset_scheduler.llm import ModelError, OpenAICompatModel, build_model  # noqa: E402
from asset_scheduler.policy import OfflinePolicyMock  # noqa: E402
from asset_scheduler.store import DataRepository  # noqa: E402
from asset_scheduler.tools import Toolbox  # noqa: E402


# ---------------------------------------------------------------------------
# scripted OpenAI-compatible server
# ---------------------------------------------------------------------------
def tool_call_response(name, arguments, call_id="call_1", content="调用工具"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            }
        ]
    }


def finish_response(content="模型结束运行。"):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _MockHandler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/1.0"

    def log_message(self, format, *args):  # silence request logging
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"_raw": raw.decode("utf-8", "replace")}
        record = {
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
        }
        self.server.requests.append(record)  # type: ignore[attr-defined]
        try:
            status, payload = self.server.respond(record)  # type: ignore[attr-defined]
        except Exception as exc:  # a broken responder must not hang the test
            status, payload = 500, {"error": {"message": "responder crashed: %r" % (exc,)}}
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _MockOpenAIServer:
    """Minimal scripted OpenAI-compatible endpoint on 127.0.0.1."""

    def __init__(self, responses):
        self.requests = []
        self._responses = list(responses)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self.httpd.daemon_threads = True
        self.httpd.requests = self.requests
        self.httpd.respond = self._respond
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _respond(self, record):
        index = len(self.requests) - 1
        if self._responses:
            response = self._responses[index] if index < len(self._responses) else self._responses[-1]
        else:
            response = finish_response()
        if callable(response):
            response = response(record)
        if isinstance(response, tuple):
            return response
        return 200, response

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/v1" % (host, port)


class LLMAdapterTests(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        # Loopback traffic must never go through a configured proxy.
        proxy_patch = mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"})
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)

    # ------------------------------------------------------------------ helpers
    def start_server(self, responses):
        server = _MockOpenAIServer(responses)
        server.start()
        self.addCleanup(server.stop)
        return server

    def make_model(self, server, *, api_key="sk-test-key", model_name="test-model", timeout_sec=5.0):
        return OpenAICompatModel(
            api_key=api_key,
            base_url=server.base_url,
            model=model_name,
            temperature=0.0,
            timeout_sec=timeout_sec,
        )

    @staticmethod
    def agent_state(asset_id="M01"):
        return AgentState(request=Request(request_id="req-adapter", asset_ids=[asset_id]))

    # ----------------------------------------------------------- request shape
    def test_tool_call_is_parsed_and_request_uses_openai_contract(self):
        server = self.start_server(
            [tool_call_response("get_asset", '{"asset_id": "M01"}', call_id="call_abc", content="先读取素材")]
        )
        model = self.make_model(server)
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "任务描述"},
        ]
        decision = model.decide(messages, Toolbox.specs(), self.agent_state())

        self.assertEqual(decision.action, "tool")
        self.assertEqual(decision.tool, "get_asset")
        self.assertEqual(decision.args, {"asset_id": "M01"})
        self.assertEqual(decision.reason, "先读取素材")

        self.assertEqual(len(server.requests), 1)
        record = server.requests[0]
        self.assertEqual(record["path"], "/v1/chat/completions")
        headers = record["headers"]
        self.assertEqual(headers.get("authorization"), "Bearer sk-test-key")
        self.assertTrue(headers.get("content-type", "").startswith("application/json"))
        self.assertIn("asset-scheduler-agent", headers.get("user-agent", ""))

        body = record["body"]
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["max_tokens"], 900)
        self.assertEqual(body["messages"], messages)

        tools = body["tools"]
        self.assertEqual(len(tools), len(TOOL_NAMES))
        self.assertEqual([entry["function"]["name"] for entry in tools], TOOL_NAMES)
        for entry in tools:
            self.assertEqual(entry["type"], "function")
            self.assertEqual(entry["function"]["parameters"]["type"], "object")
        by_name = {entry["function"]["name"]: entry["function"] for entry in tools}
        self.assertEqual(by_name["get_asset"]["parameters"]["required"], ["asset_id"])

    def test_finish_decision_when_model_returns_no_tool_calls(self):
        server = self.start_server([finish_response("完成：草稿已保存，未发布。"), finish_response("")])
        model = self.make_model(server)
        state = self.agent_state()
        first = model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), state)
        self.assertEqual(first.action, "finish")
        self.assertIn("未发布", first.reason)
        second = model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), state)
        self.assertEqual(second.action, "finish")
        self.assertEqual(second.reason, "模型结束运行。")

    def test_transcript_is_converted_to_openai_tool_call_format(self):
        server = self.start_server([finish_response("结束")])
        model = self.make_model(server)
        messages = [
            {"role": "system", "content": "系统"},
            {
                "role": "assistant",
                "content": "先读取素材",
                "tool_call": {"id": "call_1", "name": "get_asset", "arguments": {"asset_id": "M01"}},
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "get_asset", "content": '{"ok": true}'},
        ]
        decision = model.decide(messages, Toolbox.specs(), self.agent_state())
        self.assertEqual(decision.action, "finish")

        converted = server.requests[0]["body"]["messages"]
        self.assertEqual(
            converted,
            [
                {"role": "system", "content": "系统"},
                {
                    "role": "assistant",
                    "content": "先读取素材",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_asset",
                                "arguments": '{"asset_id": "M01"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "name": "get_asset", "content": '{"ok": true}'},
            ],
        )
        assistant = converted[1]
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"asset_id": "M01"})

    def test_malformed_tool_arguments_fall_back_to_empty_dict(self):
        server = self.start_server([tool_call_response("get_asset", "这不是 JSON", call_id="call_bad", content="")])
        model = self.make_model(server)
        decision = model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), self.agent_state())
        self.assertEqual(decision.action, "tool")
        self.assertEqual(decision.tool, "get_asset")
        self.assertEqual(decision.args, {})
        self.assertEqual(decision.reason, "调用工具 get_asset")

    def test_tool_arguments_accept_object_and_null_forms(self):
        server = self.start_server(
            [
                tool_call_response("get_asset", {"asset_id": "M01"}, call_id="call_obj", content="对象参数"),
                tool_call_response("get_asset", None, call_id="call_null", content="空参数"),
            ]
        )
        model = self.make_model(server)
        state = self.agent_state()
        first = model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), state)
        self.assertEqual(first.action, "tool")
        self.assertEqual(first.args, {"asset_id": "M01"})
        second = model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), state)
        self.assertEqual(second.action, "tool")
        self.assertEqual(second.args, {})

    # --------------------------------------------------------------- errors
    def test_http_error_raises_model_error(self):
        server = self.start_server([(500, {"error": {"message": "internal boom"}})])
        model = self.make_model(server)
        with self.assertRaises(ModelError) as ctx:
            model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), self.agent_state())
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_non_json_response_raises_model_error(self):
        server = self.start_server([(200, b"<html>not json</html>")])
        model = self.make_model(server)
        with self.assertRaises(ModelError) as ctx:
            model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), self.agent_state())
        self.assertIn("非 JSON", str(ctx.exception))

    def test_invalid_response_shape_raises_model_error(self):
        server = self.start_server([{"unexpected": True}])
        model = self.make_model(server)
        with self.assertRaises(ModelError) as ctx:
            model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), self.agent_state())
        self.assertIn("响应格式", str(ctx.exception))

    def test_connection_failure_raises_model_error(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        model = OpenAICompatModel(
            api_key="sk-test-key",
            base_url="http://127.0.0.1:%d/v1" % port,
            model="test-model",
            timeout_sec=2.0,
        )
        with self.assertRaises(ModelError) as ctx:
            model.decide([{"role": "user", "content": "x"}], Toolbox.specs(), self.agent_state())
        self.assertIn("连接失败", str(ctx.exception))

    # ------------------------------------------------------- model selection
    def test_build_model_auto_falls_back_to_offline_mock_without_key(self):
        model, notice = build_model(
            provider="auto",
            api_key=None,
            base_url="http://127.0.0.1:1/v1",
            model_name="deepseek-chat",
        )
        self.assertIsInstance(model, OfflinePolicyMock)
        self.assertEqual(model.name, "offline-policy-mock")
        self.assertIsNotNone(notice)
        self.assertIn("Mock", notice)

    def test_build_model_auto_prefers_real_llm_when_key_present(self):
        model, notice = build_model(
            provider="auto",
            api_key="sk-test-key",
            base_url="http://127.0.0.1:1/v1",
            model_name="deepseek-chat",
        )
        self.assertIsInstance(model, OpenAICompatModel)
        self.assertEqual(model.name, "llm:deepseek-chat")
        self.assertIsNone(notice)

    def test_build_model_explicit_llm_requires_key(self):
        with self.assertRaises(ModelError):
            build_model(
                provider="llm",
                api_key=None,
                base_url="http://127.0.0.1:1/v1",
                model_name="deepseek-chat",
            )

    def test_build_model_explicit_mock_ignores_key(self):
        model, notice = build_model(
            provider="mock",
            api_key="sk-ignored",
            base_url="http://127.0.0.1:1/v1",
            model_name="deepseek-chat",
        )
        self.assertIsInstance(model, OfflinePolicyMock)
        self.assertIsNone(notice)

    # -------------------------------------------------- end-to-end agent loop
    def test_react_agent_completes_m04_through_scripted_tool_calls(self):
        server = self.start_server(
            [
                tool_call_response("get_asset", '{"asset_id": "M04"}', call_id="c1", content="读取素材"),
                tool_call_response("check_review_rules", '{"asset_id": "M04"}', call_id="c2", content="检查规则"),
                tool_call_response(
                    "query_publish_slots",
                    '{"date": "2026-11-03", "channel": "抖音", "asset_id": "M04"}',
                    call_id="c3",
                    content="查询时段",
                ),
                tool_call_response(
                    "save_schedule_draft",
                    '{"asset_id": "M04", "date": "2026-11-03", "time": "10:00", "channel": "抖音"}',
                    call_id="c4",
                    content="保存草稿",
                ),
                finish_response("草稿已保存，未发布：等待人工确认。"),
            ]
        )
        state = self.state_subdir("llm-agent")
        repo = DataRepository(DATA_DIR, state)
        agent = ReActAgent(
            model=self.make_model(server),
            toolbox=Toolbox(repo),
            max_steps=10,
            max_tool_calls=10,
        )
        result = agent.run(Request(request_id="req-llm-agent", asset_ids=["M04"]))
        outcome = result.assets[0]

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model, "llm:test-model")
        self.assertEqual(
            [step.tool for step in result.steps if step.action == "tool"],
            ["get_asset", "check_review_rules", "query_publish_slots", "save_schedule_draft"],
        )
        self.assertEqual(result.steps[-1].action, "finish")
        self.assertEqual(result.steps[-1].reason, "草稿已保存，未发布：等待人工确认。")
        self.assertEqual(outcome.task_status, "completed")
        self.assertEqual(outcome.draft_status, "saved")
        self.assertEqual(outcome.suggested_slot, {"date": "2026-11-03", "time": "10:00", "channel": "抖音"})
        self.assertIs(outcome.published, False)
        self.assertEqual(len(active_drafts(state, "M04")), 1)
        self.assertEqual(drafts_for(state, "M04")[0]["status"], "draft")

        # Every round trip used the adapter contract.
        self.assertEqual(len(server.requests), 5)
        for record in server.requests:
            self.assertEqual(record["path"], "/v1/chat/completions")
            self.assertEqual(record["headers"].get("authorization"), "Bearer sk-test-key")
            self.assertEqual(record["body"]["model"], "test-model")
            self.assertEqual(len(record["body"]["tools"]), len(TOOL_NAMES))
        self.assertEqual([len(record["body"]["messages"]) for record in server.requests], [2, 4, 6, 8, 10])

        # Tool observations are fed back verbatim, including the draft record.
        final_messages = server.requests[-1]["body"]["messages"]
        tool_messages = [message for message in final_messages if message.get("role") == "tool"]
        # The runtime assigns its own call ids (call_1..call_N) to the
        # transcript it sends back; the ids from the mock response are not
        # echoed, but every assistant tool call is paired with its observation.
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2", "call_3", "call_4"])
        assistant_calls = [
            call["id"]
            for message in final_messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
        ]
        self.assertEqual(assistant_calls, ["call_1", "call_2", "call_3", "call_4"])
        save_observation = json.loads(tool_messages[-1]["content"])
        self.assertIs(save_observation["ok"], True)
        self.assertIs(save_observation["data"]["published"], False)
        self.assertTrue(save_observation["data"]["draft_id"].startswith("draft-"))
        self.assertEqual(tool_messages[-1]["name"], "save_schedule_draft")
        self.assertIs(save_observation["data"]["deduplicated"], False)
        self.assertEqual(save_observation["data"]["confirmation_status"], "not_required")

    def test_react_agent_guard_blocks_fixed_requirement_change(self):
        server = self.start_server(
            [
                tool_call_response("get_asset", '{"asset_id": "M06"}', call_id="c1", content="读取素材"),
                tool_call_response("check_review_rules", '{"asset_id": "M06"}', call_id="c2", content="检查规则"),
                tool_call_response(
                    "query_publish_slots",
                    '{"date": "2026-11-04", "channel": "抖音", "asset_id": "M06"}',
                    call_id="c3",
                    content="查询时段",
                ),
                tool_call_response(
                    "save_schedule_draft",
                    '{"asset_id": "M06", "date": "2026-11-04", "time": "19:00", "channel": "抖音"}',
                    call_id="c4",
                    content="尝试改到 19:00",
                ),
                finish_response("保存被工具拒绝，需用户决定。"),
            ]
        )
        state = self.state_subdir("llm-guard")
        repo = DataRepository(DATA_DIR, state)
        agent = ReActAgent(
            model=self.make_model(server),
            toolbox=Toolbox(repo),
            max_steps=10,
            max_tool_calls=10,
        )
        result = agent.run(Request(request_id="req-llm-guard", asset_ids=["M06"]))

        self.assertEqual(result.status, "completed")
        save_step = [step for step in result.steps if step.tool == "save_schedule_draft"][0]
        self.assertIs(save_step.result_ok, False)
        self.assertEqual(save_step.error_code, "HARD_REQUIREMENT_CHANGE_BLOCKED")

        outcome = result.assets[0]
        self.assertEqual(outcome.task_status, "blocked")
        self.assertEqual(outcome.draft_status, "failed")
        self.assertEqual(outcome.draft_error["code"], "HARD_REQUIREMENT_CHANGE_BLOCKED")
        self.assertIn("硬性发布要求不得擅自修改", outcome.status_reason)
        self.assertTrue(any("硬性要求 18:00" in question for question in outcome.pending_confirmations))
        self.assertIs(outcome.published, False)
        self.assertIsNone(outcome.suggested_slot)
        self.assertEqual(find_published_true(result.to_dict()), [])
        self.assertEqual(drafts_for(state), [])

        # The rejection is visible in the observation sent back to the model.
        final_messages = server.requests[-1]["body"]["messages"]
        tool_messages = [message for message in final_messages if message.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 4)
        save_observation = json.loads(tool_messages[-1]["content"])
        self.assertIs(save_observation["ok"], False)
        self.assertEqual(save_observation["error"]["code"], "HARD_REQUIREMENT_CHANGE_BLOCKED")
