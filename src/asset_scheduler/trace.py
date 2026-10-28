"""Trace recording and human-readable rendering (examples/traces).

A trace contains only observable facts: decision rationale (the short reason
the model emitted), tool name/arguments, observation summary, and the resulting
status.  It never contains hidden chain-of-thought.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .domain import Request, RunResult, StepRecord, ToolResult


def _short(value: Any, limit: int = 360) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def summarize_result(tool: str, result: ToolResult, args: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """Return (ok, one-line observation summary, error_code)."""
    if not result.ok:
        error = result.error
        code = error.code if error else "UNKNOWN"
        message = error.message if error else "工具失败"
        return False, "%s: %s" % (code, message), code

    data = result.data
    if tool == "list_pending_assets":
        ids = [a.get("asset_id") for a in data.get("assets", [])]
        return True, "待处理素材 %d 个：%s" % (len(ids), ", ".join(ids)), None
    if tool == "get_asset":
        asset = data.get("asset", {})
        missing = data.get("missing_fields", [])
        summary = "读取素材 %s（%s，%s）" % (
            asset.get("asset_id"),
            asset.get("title") or "无标题",
            asset.get("asset_type") or "未知类型",
        )
        summary += "；缺失字段：%s" % (", ".join(missing) if missing else "无")
        return True, summary, None
    if tool == "check_review_rules":
        findings = data.get("findings", [])
        blocking = [f["rule_id"] for f in findings if f.get("severity") == "blocking"]
        warns = [f["rule_id"] for f in findings if f.get("severity") == "warn"]
        summary = "审核检查：阻断 %d 项%s；待确认 %d 项%s" % (
            len(blocking),
            "（%s）" % ", ".join(blocking) if blocking else "",
            len(warns),
            "（%s）" % ", ".join(warns) if warns else "",
        )
        return True, summary, None
    if tool == "query_publish_slots":
        slots = data.get("slots", [])
        available = data.get("available", [])
        occupied = [s["time"] for s in slots if s.get("status") == "occupied"]
        summary = "时段查询 %s %s：占用 %s；可用 %s" % (
            data.get("date"),
            data.get("channel"),
            ", ".join(occupied) if occupied else "无",
            ", ".join(available) if available else "无",
        )
        return True, summary, None
    if tool == "save_schedule_draft":
        status = data.get("confirmation_status", "unknown")
        if data.get("deduplicated"):
            summary = "已存在相同草稿 %s（幂等命中，未重复创建）；确认状态=%s" % (
                data.get("draft_id"),
                status,
            )
        else:
            summary = "保存草稿 %s（%s %s %s）；确认状态=%s" % (
                data.get("draft_id"),
                data.get("date"),
                data.get("time"),
                data.get("channel"),
                status,
            )
        return True, summary, None
    if tool == "get_schedule_draft":
        return True, "读取草稿 %s：状态=%s，确认状态=%s" % (
            data.get("draft_id"),
            data.get("status"),
            data.get("confirmation_status"),
        ), None
    return True, _short(data), None


def format_slot(slot: Optional[Dict[str, Any]]) -> str:
    if not slot:
        return "—"
    return "%s %s %s" % (slot.get("date"), slot.get("time"), slot.get("channel"))


class Tracer:
    """Collects steps for one run and renders artifacts."""

    def __init__(self, run_id: str, model_name: str, request: Request):
        self.run_id = run_id
        self.model_name = model_name
        self.request = request
        self.steps: List[StepRecord] = []

    def add(self, record: StepRecord) -> None:
        self.steps.append(record)

    def finalize(self, result: RunResult) -> None:
        for index, step in enumerate(self.steps):
            if index + 1 < len(self.steps):
                nxt = self.steps[index + 1]
                if nxt.action == "finish":
                    step.next_action = "结束"
                else:
                    step.next_action = "调用 %s" % (nxt.tool or "?")
            else:
                step.next_action = "结束（%s）" % result.status

    def render_markdown(self, result: RunResult, trace_files: Optional[Dict[str, str]] = None) -> str:
        lines: List[str] = []
        lines.append("# Trace · %s" % self.request.request_id)
        lines.append("")
        lines.append("| 项目 | 内容 |")
        lines.append("| --- | --- |")
        lines.append("| run_id | `%s` |" % self.run_id)
        lines.append("| 模型后端 | `%s` |" % self.model_name)
        lines.append("| 请求模式 | `%s` |" % self.request.mode)
        lines.append("| 素材 | %s |" % (", ".join(self.request.asset_ids) or "（自动发现）"))
        lines.append("| 开始时间 | %s |" % result.started_at)
        lines.append("| 运行状态 | **%s**（%s） |" % (result.status, result.status_reason))
        limits = result.limits or {}
        lines.append(
            "| 预算 | 步数 %s/%s，工具调用 %s/%s |"
            % (
                limits.get("steps_used"),
                limits.get("max_steps"),
                limits.get("tool_calls_used"),
                limits.get("max_tool_calls"),
            )
        )
        lines.append("")
        if self.request.user_message:
            lines.append("**用户输入**：%s" % self.request.user_message)
            lines.append("")

        lines.append("## 决策与工具调用")
        lines.append("")
        lines.append("| 步 | 决策依据（模型给出） | 动作 / 工具 | 工具参数 | 观测摘要 | 结果 | 下一步 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for step in self.steps:
            if step.action == "finish":
                action = "finish"
                args_text = "—"
                result_text = "—"
                summary = step.result_summary or "结束"
            else:
                action = "tool: `%s`" % step.tool
                args_text = "`%s`" % _short(step.args, 300).replace("|", "\\|")
                summary = step.result_summary.replace("|", "\\|")
                result_text = "ok" if step.result_ok else "error: %s" % step.error_code
            lines.append(
                "| %d | %s | %s | %s | %s | %s | %s |"
                % (
                    step.index,
                    step.reason.replace("|", "\\|") or "—",
                    action,
                    args_text,
                    summary or "—",
                    result_text,
                    step.next_action or "—",
                )
            )
        lines.append("")

        lines.append("## 最终结果（由工具观测计算，不采用模型自述）")
        lines.append("")
        if result.assets:
            lines.append("| 素材 | 审核状态 | 风险原因 | 建议时段 | 待确认项 | 草稿状态 | 任务状态 | 已发布 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for asset in result.assets:
                risks = "；".join(f.get("rule_id", "") for f in asset.risk_reasons) or "无"
                confirmations = "；".join(asset.pending_confirmations) or "无"
                draft = asset.draft_status
                if asset.draft_id:
                    draft += "（%s）" % asset.draft_id
                if asset.draft_error:
                    draft += "（%s）" % asset.draft_error.get("code")
                lines.append(
                    "| %s | %s | %s | %s | %s | %s | %s | %s |"
                    % (
                        asset.asset_id,
                        asset.review_status,
                        risks,
                        format_slot(asset.suggested_slot),
                        confirmations,
                        draft,
                        asset.task_status,
                        "是" if asset.published else "否",
                    )
                )
            lines.append("")
            lines.append("任务状态说明：`%s`" % result.status_reason)
        else:
            lines.append("本次为发现运行，未进入排期。发现素材：%s" % (", ".join(result.discovered_assets) or "无"))
        lines.append("")
        if trace_files:
            lines.append("关联文件：")
            for label, path in trace_files.items():
                lines.append("- %s：`%s`" % (label, path))
            lines.append("")
        lines.append("> 本记录只包含可观测的决策依据、工具参数与返回摘要，不包含模型内部思维过程。")
        lines.append("> 所有结果均为草稿状态，**未发布**；模型不能通过任何工具触发真实发布。")
        lines.append("")
        return "\n".join(lines)

    def write(self, out_dir: Path, stem: str, result: RunResult) -> Dict[str, str]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = out_dir / ("%s.md" % stem)
        json_path = out_dir / ("%s.json" % stem)
        markdown_path.write_text(self.render_markdown(result), encoding="utf-8")
        json_path.write_text(
            json.dumps({"run": result.to_dict()}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"markdown": str(markdown_path), "json": str(json_path)}
