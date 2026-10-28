"""Case/request runner: builds the model, data repository and traces, then
aggregates run results for the CLI and the example generator.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent import ReActAgent
from .config import Settings
from .domain import Request, RunResult, utcnow_iso
from .llm import build_model
from .store import DataRepository
from .tools import Toolbox


class CaseError(Exception):
    pass


def load_case(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise CaseError("用例文件不存在：%s" % path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError("用例文件不是合法 JSON：%s（%s）" % (path, exc))
    return payload


def normalize_case(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise CaseError("用例必须是 JSON 对象。")
    case_id = str(payload.get("case_id") or "case")
    raw_requests = payload.get("requests")
    if raw_requests is None:
        raw_request = payload.get("request")
        if raw_request is None:
            raise CaseError("用例需要 request 或 requests 字段。")
        raw_requests = [raw_request]
    if not isinstance(raw_requests, list) or not raw_requests:
        raise CaseError("requests 必须是非空数组。")
    requests = [Request.from_dict(item, source=case_id) for item in raw_requests]
    return {
        "case_id": case_id,
        "title": payload.get("title", ""),
        "acceptance": list(payload.get("acceptance") or []),
        "expect": payload.get("expect") or {},
        "requests": requests,
        "run_options": payload.get("run_options") or {},
    }


class Execution:
    def __init__(self, aggregate: Dict[str, Any], traces: List[Dict[str, Any]]):
        self.aggregate = aggregate
        self.traces = traces


def execute_case(
    case: Dict[str, Any],
    settings: Settings,
    model_override: Optional[str] = None,
    state_dir: Optional[Path] = None,
) -> Execution:
    """Run a normalized case (one request or a sequential request list)."""
    state_path = Path(state_dir) if state_dir else settings.state_dir
    repo = DataRepository(settings.data_dir, state_path)
    model, notice = build_model(
        provider=model_override or settings.model_provider,
        api_key=settings.api_key,
        base_url=settings.base_url,
        model_name=settings.model_name,
        temperature=settings.temperature,
        timeout_sec=settings.timeout_sec,
    )

    run_options = case.get("run_options") or {}
    max_steps = int(run_options.get("max_steps") or settings.max_steps)
    max_tool_calls = int(run_options.get("max_tool_calls") or settings.max_tool_calls)
    requests: List[Request] = case["requests"]
    case_id = case["case_id"]

    runs: List[RunResult] = []
    traces: List[Dict[str, Any]] = []

    for request_index, request in enumerate(requests, start=1):
        if request.mode == "pending":
            discovery_agent = ReActAgent(
                model=model,
                toolbox=Toolbox(repo),
                max_steps=max_steps,
                max_tool_calls=max_tool_calls,
                run_id="run-" + uuid.uuid4().hex[:12],
            )
            discovery = discovery_agent.run(request)
            runs.append(discovery)
            traces.append(
                {
                    "stem": _stem(case_id, len(requests), request_index, "discovery"),
                    "agent": discovery_agent,
                    "result": discovery,
                }
            )
            discovered = list(discovery.discovered_assets)
            for asset_id in discovered:
                sub_request = Request(
                    request_id="%s::%s" % (request.request_id, asset_id),
                    asset_ids=[asset_id],
                    user_message=request.user_message,
                    mode="asset",
                    limit=1,
                    source=request.source,
                )
                agent = ReActAgent(
                    model=model,
                    toolbox=Toolbox(repo),
                    max_steps=max_steps,
                    max_tool_calls=max_tool_calls,
                    run_id="run-" + uuid.uuid4().hex[:12],
                )
                result = agent.run(sub_request)
                runs.append(result)
                traces.append(
                    {
                        "stem": "%s--%s" % (case_id, asset_id),
                        "agent": agent,
                        "result": result,
                    }
                )
        else:
            agent = ReActAgent(
                model=model,
                toolbox=Toolbox(repo),
                max_steps=max_steps,
                max_tool_calls=max_tool_calls,
                run_id="run-" + uuid.uuid4().hex[:12],
            )
            result = agent.run(request)
            runs.append(result)
            traces.append(
                {
                    "stem": _stem(case_id, len(requests), request_index, None),
                    "agent": agent,
                    "result": result,
                }
            )

    aggregate = aggregate_runs(case, runs, model_label=getattr(model, "name", "unknown"), notice=notice)
    return Execution(aggregate=aggregate, traces=traces)


def _stem(case_id: str, request_count: int, request_index: int, suffix: Optional[str]) -> str:
    if suffix:
        return "%s--%s" % (case_id, suffix)
    if request_count == 1:
        return case_id
    return "%s--r%d" % (case_id, request_index)


def aggregate_runs(
    case: Dict[str, Any],
    runs: List[RunResult],
    model_label: str,
    notice: Optional[str],
) -> Dict[str, Any]:
    assets = [asset for run in runs for asset in run.assets]
    task_counts: Dict[str, int] = {}
    review_counts: Dict[str, int] = {}
    for asset in assets:
        task_counts[asset.task_status] = task_counts.get(asset.task_status, 0) + 1
        review_counts[asset.review_status] = review_counts.get(asset.review_status, 0) + 1

    return {
        "case_id": case.get("case_id"),
        "title": case.get("title", ""),
        "generated_at": utcnow_iso(),
        "model": {"backend": model_label, "notice": notice},
        "requests": [request.to_dict() for request in case["requests"]],
        "run_options": case.get("run_options") or {},
        "runs": [run.to_dict() for run in runs],
        "assets": [asset.to_dict() for asset in assets],
        "summary": {
            "runs": len(runs),
            "assets": len(assets),
            "task_status": task_counts,
            "review_status": review_counts,
            "draft_saved": sum(1 for a in assets if a.draft_status in {"saved", "duplicate_existing"}),
            "pending_confirmation": sum(1 for a in assets if a.pending_confirmations),
            "run_status": _count(run.status for run in runs),
            "published": False,
        },
        "published": False,
    }


def _count(values) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def write_traces(execution: Execution, trace_dir: Path) -> List[str]:
    """Write every trace and return the paths written."""
    written: List[str] = []
    trace_dir = Path(trace_dir)
    for entry in execution.traces:
        agent: ReActAgent = entry["agent"]
        assert agent.last_tracer is not None
        paths = agent.last_tracer.write(trace_dir, entry["stem"], entry["result"])
        written.extend([paths["markdown"], paths["json"]])
    return written
