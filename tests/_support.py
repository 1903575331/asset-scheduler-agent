"""Shared helpers for the acceptance test suite (Python stdlib only).

Environment note
----------------
This DSH sandbox may lock down directories created inside the system Temp
directory, so every test uses a *plain* directory created with ``mkdir`` under
``<repo>/.tmp-tests/`` and removes it again from ``addCleanup``.  No test touches
the repository's ``data/`` directory directly; when a scenario needs different
data a copy is placed inside the per-test scratch directory.

All tests run offline against the offline deterministic policy mock, except the
LLM adapter tests which talk to a loopback-only ``ThreadingHTTPServer``.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
SRC_DIR = ROOT / "src"
DATA_DIR = ROOT / "data"
SCRATCH_ROOT = ROOT / ".tmp-tests"

for _path in (str(SRC_DIR), str(ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from asset_scheduler.config import Settings  # noqa: E402
from asset_scheduler.domain import AssetOutcome, Decision, Request, RunResult  # noqa: E402
from asset_scheduler.runner import Execution, execute_case, normalize_case  # noqa: E402
from asset_scheduler.tools import Toolbox  # noqa: E402

TASK_STATUSES = {
    "completed",
    "completed_pending_confirmation",
    "blocked",
    "needs_input",
    "failed",
    "incomplete",
}
DRAFT_STATUSES = {"saved", "duplicate_existing", "failed", "not_attempted"}
TOOL_NAMES = [spec["name"] for spec in Toolbox.specs()]


# ---------------------------------------------------------------------------
# scratch directories (never use tempfile: the sandbox may lock system Temp)
# ---------------------------------------------------------------------------
def new_scratch_dir(prefix: str = "case") -> Path:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_ROOT / ("%s-%s" % (prefix, uuid.uuid4().hex[:10]))
    path.mkdir(parents=True, exist_ok=False)
    return path


def remove_scratch_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class WorkspaceTestCase(unittest.TestCase):
    """Base class providing an isolated scratch directory and state dir."""

    def setUp(self) -> None:
        self.case_dir = new_scratch_dir(type(self).__name__.lower()[:24])
        self.addCleanup(remove_scratch_dir, self.case_dir)
        self.tmp = self.case_dir
        self.state_dir = self.case_dir / "state"

    def copy_data_dir(self, name: str = "data") -> Path:
        """Copy the repository data directory into the scratch area."""
        destination = self.case_dir / name
        shutil.copytree(DATA_DIR, destination)
        return destination

    def state_subdir(self, name: str) -> Path:
        return self.case_dir / ("state-" + name)


# ---------------------------------------------------------------------------
# settings / case helpers
# ---------------------------------------------------------------------------
def make_settings(
    state_dir: Path,
    data_dir: Optional[Path] = None,
    *,
    model_provider: str = "mock",
    max_steps: int = 14,
    max_tool_calls: int = 16,
    trace_dir: Optional[Path] = None,
) -> Settings:
    settings = Settings.from_env(
        project_root=ROOT,
        state_dir=Path(state_dir),
        trace_dir=trace_dir,
        model_provider=model_provider,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
    )
    if data_dir is not None:
        settings = dataclasses.replace(settings, data_dir=Path(data_dir))
    return settings


def asset_case(
    asset_id: str,
    *,
    case_id: Optional[str] = None,
    request_id: Optional[str] = None,
    run_options: Optional[Dict[str, Any]] = None,
    user_message: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "case_id": case_id or ("case-" + asset_id),
        "request": {
            "request_id": request_id or ("req-" + asset_id),
            "asset_ids": [asset_id],
            "user_message": user_message,
        },
    }
    if run_options:
        payload["run_options"] = dict(run_options)
    return normalize_case(payload)


def run_asset(
    asset_id: str,
    state_dir: Path,
    *,
    data_dir: Optional[Path] = None,
    model_provider: str = "mock",
    max_steps: int = 14,
    max_tool_calls: int = 16,
    run_options: Optional[Dict[str, Any]] = None,
) -> Execution:
    settings = make_settings(
        state_dir,
        data_dir,
        model_provider=model_provider,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
    )
    case = asset_case(asset_id, run_options=run_options)
    return execute_case(case, settings, state_dir=Path(state_dir))


def last_run(execution: Execution) -> RunResult:
    return execution.traces[-1]["result"]


def outcome_for(execution: Execution, asset_id: str) -> AssetOutcome:
    for outcome in last_run(execution).assets:
        if outcome.asset_id == asset_id:
            return outcome
    raise AssertionError("execution 中没有素材 %s 的结果" % asset_id)


def tool_sequence(run: RunResult) -> List[str]:
    return [step.tool for step in run.steps if step.action == "tool"]


# ---------------------------------------------------------------------------
# draft store inspection
# ---------------------------------------------------------------------------
def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def drafts_doc(state_dir: Path) -> Dict[str, Any]:
    path = Path(state_dir) / "drafts.json"
    if not path.is_file():
        return {"drafts": {}, "meta": {"fault_attempts": {}}}
    return read_json(path)


def drafts_for(state_dir: Path, asset_id: Optional[str] = None) -> List[Dict[str, Any]]:
    doc = drafts_doc(state_dir)
    result = []
    for draft in (doc.get("drafts") or {}).values():
        if asset_id is None or draft.get("asset_id") == asset_id:
            result.append(draft)
    return result


def active_drafts(state_dir: Path, asset_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return [draft for draft in drafts_for(state_dir, asset_id) if draft.get("status") == "draft"]


# ---------------------------------------------------------------------------
# assertions shared by several test modules
# ---------------------------------------------------------------------------
def find_published_true(value: Any, path: str = "root") -> List[str]:
    """Return JSON paths where ``published`` is literally ``True``."""
    hits: List[str] = []
    if isinstance(value, dict):
        if value.get("published") is True:
            hits.append(path)
        for key, item in value.items():
            hits.extend(find_published_true(item, "%s.%s" % (path, key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hits.extend(find_published_true(item, "%s[%d]" % (path, index)))
    return hits


def scan_text_for_success_claim(text: str) -> List[str]:
    """Success phrasings that a failed-save run must never use."""
    claims = ["已发布", "发布成功", "保存成功", "已上线"]
    return [claim for claim in claims if claim in text]


def fake_model(decisions: Iterable[Any], name: str = "scripted-model") -> Any:
    """A minimal model double used for agent guardrail tests."""

    class _ScriptedModel:
        def __init__(self) -> None:
            self.name = name
            self._decisions = list(decisions)
            self.calls = 0

        def decide(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], state: Any) -> Any:
            index = self.calls
            self.calls += 1
            if index < len(self._decisions):
                decision = self._decisions[index]
            else:
                decision = Decision(action="finish", reason="脚本结束。")
            if isinstance(decision, BaseException):
                raise decision
            return decision

    return _ScriptedModel()


def single_asset_request(asset_id: str, request_id: Optional[str] = None) -> Request:
    return Request(request_id=request_id or ("req-" + asset_id), asset_ids=[asset_id])
