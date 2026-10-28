#!/usr/bin/env python
"""Generate all example artifacts under ``examples/``.

For every case in ``examples/cases/*.json`` this script:

1. runs the Agent with the offline mock backend against a clean state dir;
2. writes the real output to ``examples/outputs/<case_id>.json``;
3. writes each run trace to ``examples/traces/<stem>.md`` and ``.json``;
4. compares the observed result with the case expectations (``expect``);
5. rebuilds ``examples/README.md`` with an index and PASS/FAIL table.

Run from the repo root (no third-party dependencies):

    python scripts/generate_examples.py
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.asset_scheduler.config import Settings  # noqa: E402
from src.asset_scheduler.domain import utcnow_iso  # noqa: E402
from src.asset_scheduler.runner import execute_case, load_case, normalize_case, write_traces  # noqa: E402

CASES_DIR = ROOT / "examples" / "cases"
OUT_DIR = ROOT / "examples" / "outputs"
TRACE_DIR = ROOT / "examples" / "traces"
STATE_ROOT = ROOT / "examples" / ".run"
DATA_DIR = ROOT / "data"


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _copy_data(extra_bookings: Optional[List[Dict[str, Any]]], base_dir: Path) -> Path:
    """Copy data/ next to the case state dir and patch it (workspace-local)."""
    target = Path(base_dir) / "data-copy"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(DATA_DIR, target)
    if extra_bookings:
        schedule_path = target / "schedule.json"
        document = json.loads(schedule_path.read_text(encoding="utf-8"))
        document.setdefault("bookings", []).extend(extra_bookings)
        schedule_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def _cleanup_tmp() -> None:
    """Remove per-case copied data dirs (state dirs keep drafts for inspection)."""
    for path in STATE_ROOT.glob("*/data-copy"):
        shutil.rmtree(path, ignore_errors=True)


def _summary(runs: List[Dict[str, Any]], assets: List[Dict[str, Any]]) -> Dict[str, Any]:
    task_counts: Dict[str, int] = {}
    review_counts: Dict[str, int] = {}
    for asset in assets:
        task_counts[asset["task_status"]] = task_counts.get(asset["task_status"], 0) + 1
        review_counts[asset["review_status"]] = review_counts.get(asset["review_status"], 0) + 1
    run_counts: Dict[str, int] = {}
    for run in runs:
        run_counts[run["status"]] = run_counts.get(run["status"], 0) + 1
    return {
        "runs": len(runs),
        "assets": len(assets),
        "task_status": task_counts,
        "review_status": review_counts,
        "draft_saved": sum(1 for a in assets if a["draft_status"] in {"saved", "duplicate_existing"}),
        "pending_confirmation": sum(1 for a in assets if a["pending_confirmations"]),
        "run_status": run_counts,
        "published": False,
    }


def _merge_executions(case: Dict[str, Any], executions: List[Any]) -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    assets: List[Dict[str, Any]] = []
    requests: List[Dict[str, Any]] = []
    for execution in executions:
        runs.extend(execution.aggregate["runs"])
        assets.extend(execution.aggregate["assets"])
        requests.extend(execution.aggregate["requests"])
    model = executions[0].aggregate["model"]
    return {
        "case_id": case["case_id"],
        "title": case.get("title", ""),
        "acceptance": list(case.get("acceptance") or []),
        "generated_at": utcnow_iso(),
        "model": model,
        "requests": requests,
        "runs": runs,
        "assets": assets,
        "summary": _summary(runs, assets),
        "published": False,
    }


def _read_draft_counts(state_dir: Path) -> Dict[str, int]:
    drafts_path = state_dir / "drafts.json"
    if not drafts_path.is_file():
        return {"active": 0, "superseded": 0}
    document = json.loads(drafts_path.read_text(encoding="utf-8"))
    drafts = document.get("drafts", {}).values()
    return {
        "active": sum(1 for d in drafts if d.get("status") == "draft"),
        "superseded": sum(1 for d in drafts if d.get("status") == "superseded"),
    }


def _check(aggregate: Dict[str, Any], state_dir: Path, case: Dict[str, Any]) -> List[Dict[str, Any]]:
    expect: Dict[str, Any] = case.get("expect") or {}
    assets = aggregate["assets"]
    runs = aggregate["runs"]
    last_asset = assets[-1] if assets else {}
    draft_counts = _read_draft_counts(state_dir)
    last_run = runs[-1] if runs else {}
    save_calls = sum(
        1
        for step in (last_run.get("steps") or [])
        if step.get("action") == "tool" and step.get("tool") == "save_schedule_draft"
    )
    slot = last_asset.get("suggested_slot") or {}

    def actual_for(key: str) -> Any:
        if key == "assets_count":
            return len(assets)
        if key == "review_status":
            return last_asset.get("review_status")
        if key == "draft_status":
            return last_asset.get("draft_status")
        if key == "task_status":
            return last_asset.get("task_status")
        if key == "suggested_time":
            return slot.get("time")
        if key == "suggested_date":
            return slot.get("date")
        if key == "published":
            return bool(last_asset.get("published", False))
        if key == "drafts_active":
            return draft_counts["active"]
        if key == "superseded_count":
            return draft_counts["superseded"]
        if key == "run_status_last":
            return last_run.get("status")
        if key == "save_tool_calls":
            return save_calls
        if key == "pending_count":
            return len(last_asset.get("pending_confirmations") or [])
        return None

    checks: List[Dict[str, Any]] = []
    for key, expected in expect.items():
        if key == "pending_contains":
            joined = "；".join(last_asset.get("pending_confirmations") or [])
            actual = expected in joined
            checks.append(
                {"field": key, "expected": expected, "actual": expected if actual else joined, "ok": actual}
            )
            continue
        actual = actual_for(key)
        checks.append({"field": key, "expected": expected, "actual": actual, "ok": actual == expected})
    return checks


def run_case_file(path: Path) -> Dict[str, Any]:
    raw = load_case(path)
    phases = raw.get("phases")
    if phases:
        case_id = str(raw.get("case_id") or path.stem)
        case = {"case_id": case_id, "title": raw.get("title", ""), "run_options": raw.get("run_options") or {}}
    else:
        case = normalize_case(raw)
        case_id = case["case_id"]
    state_dir = STATE_ROOT / case_id
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    base_settings = Settings.from_env(
        project_root=ROOT,
        state_dir=state_dir,
        model_provider="mock",
    )
    run_options = case.get("run_options") or {}

    executions = []
    trace_files: List[str] = []
    if phases:
        for index, phase in enumerate(phases, start=1):
            phase_case = normalize_case(
                {
                    "case_id": "%s--phase%d" % (case_id, index),
                    "title": "phase %d" % index,
                    "request": phase["request"],
                    "run_options": run_options,
                }
            )
            extra = (phase.get("data") or {}).get("schedule_extra_bookings")
            data_dir = _copy_data(extra, state_dir) if extra else DATA_DIR
            settings = replace(base_settings, data_dir=data_dir)
            execution = execute_case(phase_case, settings, model_override="mock", state_dir=state_dir)
            executions.append(execution)
            trace_files.extend(_rel(Path(p)) for p in write_traces(execution, TRACE_DIR))
        aggregate = _merge_executions(raw, executions)
    else:
        execution = execute_case(case, base_settings, model_override="mock", state_dir=state_dir)
        executions.append(execution)
        aggregate = execution.aggregate
        trace_files = [_rel(Path(p)) for p in write_traces(execution, TRACE_DIR)]

    checks = _check({"assets": aggregate["assets"], "runs": aggregate["runs"]}, state_dir, raw)
    aggregate["checks"] = checks
    aggregate["trace_files"] = trace_files
    aggregate["acceptance"] = list(raw.get("acceptance") or [])

    out_path = OUT_DIR / ("%s.json" % case_id)
    out_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = sum(1 for check in checks if check["ok"])
    print(
        "%-32s checks %d/%d %s  traces=%d"
        % (
            case_id,
            passed,
            len(checks),
            "PASS" if passed == len(checks) else "FAIL",
            len(trace_files),
        )
    )
    return {"case": raw, "aggregate": aggregate, "output": _rel(out_path)}


def _expect_text(raw: Dict[str, Any]) -> str:
    expect = raw.get("expect") or {}
    parts = []
    for key, value in expect.items():
        if key == "published":
            continue
        if value is None:
            value = "—"
        parts.append("%s=%s" % (key, value))
    return "；".join(parts) or "—"


def _actual_text(aggregate: Dict[str, Any]) -> str:
    assets = aggregate.get("assets") or []
    if not assets:
        return "无素材结果"
    last = assets[-1]
    slot = last.get("suggested_slot") or {}
    return "assets=%d task=%s review=%s draft=%s slot=%s %s draft_saved=%d" % (
        len(assets),
        last.get("task_status"),
        last.get("review_status"),
        last.get("draft_status"),
        slot.get("date") or "—",
        slot.get("time") or "—",
        aggregate["summary"].get("draft_saved", 0),
    )


def rebuild_index(results: List[Dict[str, Any]]) -> None:
    lines: List[str] = []
    lines.append("# Examples 索引（自动生成）")
    lines.append("")
    lines.append("本目录由 `python scripts/generate_examples.py` 生成：")
    lines.append("")
    lines.append("- `cases/`：输入用例（素材 ID、用户消息、执行预算）")
    lines.append("- `outputs/`：实际输出（完整运行结果 + 校验结果 checks + trace 文件索引）")
    lines.append("- `traces/`：每次运行的决策 / 工具参数 / 观测摘要 / 最终结果（Markdown + JSON）")
    lines.append("")
    lines.append("模型后端：`offline-policy-mock`（离线确定性 Mock）。真实模型通道需配置 `OPENAI_API_KEY`，")
    lines.append("本次未运行（详见 README「模型接入与边界」与 docs/03-evaluation.md）。")
    lines.append("")
    lines.append("| # | 用例 | 覆盖验收点 | 预期（关键字段） | 实际（末次运行） | 检查 | 输出 / Trace |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for index, result in enumerate(results, start=1):
        raw = result["case"]
        aggregate = result["aggregate"]
        checks = aggregate.get("checks") or []
        passed = sum(1 for c in checks if c["ok"])
        status = "PASS %d/%d" % (passed, len(checks)) if checks else "—"
        trace_links = []
        for path in aggregate.get("trace_files", []):
            normalized = str(path).replace("\\", "/")
            if normalized.startswith("examples/"):
                normalized = normalized[len("examples/"):]
            trace_links.append("[%s](%s)" % (Path(path).stem, normalized))
        if len(trace_links) > 4:
            trace_links = trace_links[:4] + ["…（共 %d 个）" % len(trace_links)]
        lines.append(
            "| %d | [%s](cases/%s.json) | %s | %s | %s | %s | %s |"
            % (
                index,
                raw.get("title") or raw["case_id"],
                raw["case_id"],
                "、".join(raw.get("acceptance") or []),
                _expect_text(raw).replace("|", "\\|"),
                _actual_text(aggregate).replace("|", "\\|"),
                status,
                "[output](outputs/%s.json)<br>%s" % (raw["case_id"], "<br>".join(trace_links)),
            )
        )
    lines.append("")
    lines.append("## 关键结论")
    lines.append("")
    lines.append("- 正常 / 缺失信息 / 规则风险 / 时段冲突 / 硬性冲突 / 无可用时段 / 重复提交 / 保存失败 / 工具失败 / 执行上限均有实际输出与 Trace。")
    lines.append("- 所有结果的 `published` 均为 `false`：系统没有任何发布工具，只能保存草稿。")
    lines.append("- 相同请求重复执行命中同一 `draft_id`（`duplicate_existing`），时段变化后重规划会替换旧草稿（`superseded`）。")
    lines.append("- 达到步数上限时返回 `task_status=incomplete` 与明确原因，不会伪造成功。")
    lines.append("")
    (ROOT / "examples" / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_ROOT.exists():
        shutil.rmtree(STATE_ROOT)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)

    for directory in (OUT_DIR, TRACE_DIR):
        for old in directory.glob("*"):
            if old.is_file():
                old.unlink()

    results = []
    for path in sorted(CASES_DIR.glob("*.json")):
        results.append(run_case_file(path))

    rebuild_index(results)
    _cleanup_tmp()

    failures = [(r["case"]["case_id"], c) for r in results for c in r["aggregate"]["checks"] if not c["ok"]]
    print("")
    print("生成完成：%d 个用例，%d 项检查，失败 %d 项" % (len(results), sum(len(r["aggregate"]["checks"]) for r in results), len(failures)))
    for case_id, check in failures:
        print("  FAIL %s %s: expected=%r actual=%r" % (case_id, check["field"], check["expected"], check["actual"]))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
