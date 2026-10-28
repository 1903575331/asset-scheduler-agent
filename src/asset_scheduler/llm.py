"""Real-model adapter: OpenAI-compatible Chat Completions with tool calling.

Works with DeepSeek / Qwen / Moonshot / OpenAI-compatible gateways.  The runtime
transcript is provider-agnostic; this adapter converts it to the OpenAI format
and back.  If no credential is configured the CLI falls back to the offline
policy mock (see ``llm.build_model``).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .domain import AgentState, Decision
from .policy import OfflinePolicyMock


class ModelError(Exception):
    """Raised when the model backend cannot return a usable decision."""


class OpenAICompatModel:
    """Calls ``POST {base_url}/chat/completions`` with function tools."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        timeout_sec: float = 60.0,
        max_tokens: int = 900,
    ):
        if not api_key:
            raise ModelError("OPENAI_API_KEY 未配置，无法使用真实模型通道。")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.temperature = temperature
        self.timeout_sec = timeout_sec
        self.max_tokens = max_tokens
        self.name = "llm:%s" % model

    def decide(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], state: AgentState) -> Decision:
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": self._to_openai_messages(messages),
            "tools": [{"type": "function", "function": spec} for spec in tools],
            "tool_choice": "auto",
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        body = self._post(payload)

        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("模型响应格式不符合 OpenAI 兼容协议：%s" % _snippet(body))

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            call = tool_calls[0] or {}
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_args = function.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            if not isinstance(args, dict):
                args = {}
            reason = str(message.get("content") or "").strip()
            if not reason:
                reason = "调用工具 %s" % name
            return Decision(action="tool", reason=reason[:300], tool=name, args=args)

        content = str(message.get("content") or "").strip()
        return Decision(action="finish", reason=content[:300] or "模型结束运行。")

    # ------------------------------------------------------------------ http
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url + "/chat/completions"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "User-Agent": "asset-scheduler-agent/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelError("模型接口返回 HTTP %s：%s" % (exc.code, _snippet(detail)))
        except urllib.error.URLError as exc:
            raise ModelError("模型接口连接失败：%s" % exc.reason)
        except TimeoutError:
            raise ModelError("模型接口超时（%.0fs）。" % self.timeout_sec)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ModelError("模型接口返回非 JSON 内容：%s" % _snippet(raw))

    # ------------------------------------------------------------ transcript
    @staticmethod
    def _to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "assistant" and message.get("tool_call"):
                call = message["tool_call"]
                converted.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": [
                            {
                                "id": call.get("id") or "call_0",
                                "type": "function",
                                "function": {
                                    "name": call.get("name"),
                                    "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
            elif role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.get("tool_call_id") or "call_0",
                        "name": message.get("name"),
                        "content": message.get("content") or "",
                    }
                )
            else:
                converted.append({"role": role, "content": message.get("content") or ""})
        return converted


def _snippet(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_model(
    provider: str,
    api_key: Optional[str],
    base_url: str,
    model_name: str,
    temperature: float = 0.0,
    timeout_sec: float = 60.0,
) -> Tuple[Any, Optional[str]]:
    """Return (model, notice).  ``auto`` prefers the LLM when a key exists."""
    provider = (provider or "auto").lower()
    if provider == "llm":
        return (
            OpenAICompatModel(api_key or "", base_url, model_name, temperature, timeout_sec),
            None,
        )
    if provider == "mock":
        return OfflinePolicyMock(), None
    if api_key:
        return OpenAICompatModel(api_key, base_url, model_name, temperature, timeout_sec), None
    return (
        OfflinePolicyMock(),
        "未检测到 OPENAI_API_KEY（或 DEEPSEEK_API_KEY），已使用离线确定性 Mock 决策器；"
        "Mock 仅验证流程与安全边界，不代表真实模型的决策效果。",
    )
