"""The ReAct runtime: model decision -> tool execution -> observation -> next decision.

The loop enforces step/tool-call budgets and produces an explicit ``incomplete``
status when a budget is exhausted.  Final outcomes are computed from the tool
observations (``build_outcome``); a model can suggest but never assert a saved or
published state.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .domain import (
    AgentState,
    AssetOutcome,
    AssetState,
    Decision,
    Request,
    RunResult,
    StepRecord,
    ToolResult,
    describe_evidence,
    utcnow_iso,
)
from .llm import ModelError
from .store import DataRepository
from .tools import Toolbox
from .trace import Tracer, summarize_result

SYSTEM_PROMPT = """你是“素材审核与发布排期 Agent”，为内容团队找出待处理素材，并给出无冲突、可确认的发布**草稿**排期。

必须遵守的规则：
1. 事实只能来自工具。排期前必须读取素材（get_asset）、检查审核规则（check_review_rules）、查询时段（query_publish_slots），禁止编造素材信息、审核结果或时段。
2. 素材必填字段缺失时，停止排期并请求补充信息，不得猜测。
3. 系统中只有“保存草稿”能力，没有发布能力。任何情况下都不得声称“已发布”，最终结果只能是草稿。
4. fixed（硬性）发布时间不得修改；与已有排期冲突时必须停止并请求用户决定，不得擅自换时段。
5. fixed 以外的 preferred 要求冲突时，可以选择最近的可用时段，但保存后必须处于“待确认”状态；open 要求可自行选择可用时段。
6. 保存失败必须如实报告失败，禁止声称保存成功。仅对 retryable=true 的存储错误允许重试一次。
7. 每次模型响应只调用一个工具；信息足够时不再调用工具，用简短中文说明结束运行（不要输出内部思维过程）。
8. 有步数与工具调用次数上限。达到上限会被系统判定为“未完成”，因此请高效行动，不要重复调用相同工具获取相同信息。

结束时请用 1-3 句话概括做了什么、当前状态和待确认项。"""


def build_task_message(request: Request, repo: DataRepository) -> str:
    lines: List[str] = []
    lines.append("请求 ID：%s" % request.request_id)
    lines.append("模式：%s" % ("发现待处理素材" if request.mode == "pending" else "审核并排期"))
    if request.mode == "asset":
        lines.append("目标素材：%s" % ", ".join(request.asset_ids))
        lines.append("请从读取素材开始，依次完成审核、时段查询与草稿保存。")
    else:
        lines.append("请先调用 list_pending_assets 找出待处理素材（最多 %d 个）。" % request.limit)
    if request.user_message:
        lines.append("用户消息：%s" % request.user_message)
    lines.append(
        "交付要求：每项素材给出审核状态、风险原因、建议时段、待确认项、草稿保存状态；"
        "只保存草稿，不发布；资料不足、规则阻断或时段冲突时说明原因与待确认项。"
    )
    return "\n".join(lines)


class ReActAgent:
    """A single-agent ReAct loop bounded by step and tool-call budgets."""

    def __init__(
        self,
        model: Any,
        toolbox: Toolbox,
        max_steps: int = 14,
        max_tool_calls: int = 16,
        run_id: Optional[str] = None,
    ):
        self.model = model
        self.toolbox = toolbox
        self.max_steps = max(1, int(max_steps))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.run_id = run_id or ("run-" + uuid.uuid4().hex[:12])
        self.last_tracer: Optional[Tracer] = None
        self.last_state: Optional[AgentState] = None

    # ------------------------------------------------------------------- run
    def run(self, request: Request) -> RunResult:
        started_at = utcnow_iso()
        started_monotonic = time.monotonic()
        tracer = Tracer(self.run_id, getattr(self.model, "name", "unknown-model"), request)
        state = AgentState(request=request)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_task_message(request, self.toolbox.repo)},
        ]
        status = "completed"
        status_reason = ""
        tool_calls_used = 0

        try:
            while True:
                if len(tracer.steps) >= self.max_steps:
                    status = "incomplete"
                    status_reason = "达到步数上限 %d，任务未完成。" % self.max_steps
                    break

                decision = self.model.decide(messages, Toolbox.specs(), state)
                if not isinstance(decision, Decision):
                    raise ModelError("模型返回了非 Decision 对象：%r" % (decision,))

                if decision.action == "finish":
                    tracer.add(
                        StepRecord(
                            index=len(tracer.steps) + 1,
                            reason=decision.reason,
                            action="finish",
                            result_summary="结束：%s" % (decision.reason or "模型结束运行"),
                        )
                    )
                    state.final_reason = decision.reason
                    messages.append({"role": "assistant", "content": decision.reason})
                    status_reason = decision.reason or "运行结束"
                    break

                if decision.action != "tool":
                    status = "failed"
                    status_reason = "模型返回了非法动作：%r" % (decision.action,)
                    tracer.add(
                        StepRecord(
                            index=len(tracer.steps) + 1,
                            reason=decision.reason,
                            action="invalid",
                            result_summary=status_reason,
                        )
                    )
                    break

                if tool_calls_used >= self.max_tool_calls:
                    status = "incomplete"
                    status_reason = "达到工具调用上限 %d，任务未完成。" % self.max_tool_calls
                    tracer.add(
                        StepRecord(
                            index=len(tracer.steps) + 1,
                            reason=decision.reason,
                            action="budget_exhausted",
                            tool=decision.tool,
                            args=decision.args,
                            result_summary=status_reason,
                        )
                    )
                    break

                tool_started = time.monotonic()
                result = self.toolbox.call(decision.tool or "", decision.args)
                duration_ms = int((time.monotonic() - tool_started) * 1000)
                tool_calls_used += 1
                self._apply_result(state, decision.tool or "", decision.args, result)
                ok, summary, error_code = summarize_result(decision.tool or "", result, decision.args)
                index = len(tracer.steps) + 1
                tracer.add(
                    StepRecord(
                        index=index,
                        reason=decision.reason,
                        action="tool",
                        tool=decision.tool,
                        args=decision.args,
                        result_ok=ok,
                        result_summary=summary,
                        error_code=error_code,
                        duration_ms=duration_ms,
                    )
                )
                call_id = "call_%d" % index
                messages.append(
                    {
                        "role": "assistant",
                        "content": decision.reason,
                        "tool_call": {"id": call_id, "name": decision.tool, "arguments": decision.args},
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": decision.tool,
                        "content": json.dumps(result.to_dict(), ensure_ascii=False),
                    }
                )
        except ModelError as exc:
            status = "failed"
            status_reason = "模型调用失败：%s" % exc
        except Exception as exc:  # defensive: never crash without a run record
            status = "failed"
            status_reason = "运行异常：%s: %s" % (type(exc).__name__, exc)

        finished_at = utcnow_iso()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        outcomes: List[AssetOutcome] = []
        if request.mode == "asset":
            for asset_id in request.asset_ids:
                outcomes.append(
                    build_outcome(
                        state=state,
                        asset_id=asset_id,
                        run_status=status,
                        run_reason=status_reason,
                        steps_used=len(tracer.steps),
                        tool_calls=tool_calls_used,
                    )
                )

        discovered = [str(a.get("asset_id")) for a in (state.candidates or [])] if request.mode == "pending" else []

        result = RunResult(
            run_id=self.run_id,
            request=request,
            model=getattr(self.model, "name", "unknown-model"),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=status,
            status_reason=status_reason,
            limits={
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "steps_used": len(tracer.steps),
                "tool_calls_used": tool_calls_used,
            },
            steps=list(tracer.steps),
            assets=outcomes,
            discovered_assets=discovered,
        )
        tracer.finalize(result)
        self.last_tracer = tracer
        self.last_state = state
        return result

    # --------------------------------------------------------- observations
    def _apply_result(self, state: AgentState, tool: str, args: Dict[str, Any], result: ToolResult) -> None:
        if tool == "list_pending_assets":
            if result.ok:
                state.candidates = list(result.data.get("assets") or [])
            return

        asset_id = str(args.get("asset_id") or state.current_asset_id or "")
        if tool == "get_asset":
            st = state.ensure_asset(asset_id)
            if result.ok:
                st.asset = result.data.get("asset")
                st.missing_fields = list(result.data.get("missing_fields") or [])
            else:
                st.tool_errors.append(result.error.to_dict() if result.error else {})
            return

        if tool == "check_review_rules":
            st = state.ensure_asset(asset_id)
            if result.ok:
                st.rules = result.data
            else:
                st.tool_errors.append(result.error.to_dict() if result.error else {})
            return

        if tool == "query_publish_slots":
            st = state.ensure_asset(asset_id)
            if result.ok:
                st.slots = result.data
                st.slot_error = None
            else:
                st.slot_error = result.error.to_dict() if result.error else {}
            return

        if tool == "save_schedule_draft":
            st = state.ensure_asset(asset_id)
            st.save_attempts += 1
            if result.ok:
                st.draft = result.data
                st.last_save_error = None
            else:
                st.last_save_error = result.error.to_dict() if result.error else {}
            return

        if tool == "get_schedule_draft":
            if result.ok:
                draft = dict(result.data)
                st = state.ensure_asset(str(draft.get("asset_id") or asset_id))
                # Reading the stored record must not erase the transient
                # idempotency flag observed on save (duplicate submission).
                if st.draft and st.draft.get("deduplicated"):
                    draft["deduplicated"] = True
                st.draft = draft
                st.draft_verified = True
            return


# ---------------------------------------------------------------------------
# Result derivation
# ---------------------------------------------------------------------------
def build_outcome(
    state: AgentState,
    asset_id: str,
    run_status: str,
    run_reason: str,
    steps_used: int,
    tool_calls: int,
) -> AssetOutcome:
    st = state.assets.get(asset_id)
    outcome = AssetOutcome(asset_id=asset_id, steps_used=steps_used, tool_calls=tool_calls, published=False)
    if st is None:
        outcome.task_status = "incomplete" if run_status == "incomplete" else "failed"
        outcome.status_reason = run_reason or "未开始处理该素材。"
        return outcome

    asset = st.asset or {}
    outcome.title = asset.get("title")
    findings = list((st.rules or {}).get("findings") or [])
    outcome.risk_reasons = findings

    # A model may call the rule checker before get_asset; derive "missing
    # fields" from the R-MISS-01 finding so the outcome does not depend on the
    # model's tool ordering.
    if not st.missing_fields and st.rules:
        for finding in findings:
            if finding.get("rule_id") == "R-MISS-01":
                st.missing_fields = list((finding.get("evidence") or {}).get("missing_fields") or [])

    if st.missing_fields:
        outcome.review_status = "needs_input"
        if not findings:
            outcome.risk_reasons = [
                {
                    "rule_id": "R-MISS-01",
                    "name": "素材信息完整性",
                    "severity": "blocking",
                    "message": "排期必需字段缺失，必须先补齐。",
                    "evidence": {"missing_fields": list(st.missing_fields)},
                }
            ]
    elif st.rules is None:
        outcome.review_status = "unverified"
    elif st.rules.get("blocked"):
        outcome.review_status = "blocked"
    elif st.rules.get("needs_confirmation"):
        outcome.review_status = "needs_confirmation"
    else:
        outcome.review_status = "approved"

    draft = st.draft
    if draft:
        outcome.draft_status = "duplicate_existing" if draft.get("deduplicated") else "saved"
        outcome.draft_id = draft.get("draft_id")
        outcome.suggested_slot = {
            "date": draft.get("date"),
            "time": draft.get("time"),
            "channel": draft.get("channel"),
        }
        if draft.get("confirmation_status") == "pending_confirmation":
            outcome.pending_confirmations = list(draft.get("open_questions") or [])
    elif st.last_save_error:
        outcome.draft_status = "failed"
        outcome.draft_error = st.last_save_error
        proposal = propose_slot(st)
        if proposal:
            outcome.suggested_slot = proposal
    else:
        proposal = propose_slot(st)
        if proposal:
            outcome.suggested_slot = proposal

    requirement = asset.get("publish_requirement") or {}
    flexibility = requirement.get("flexibility")
    requested_time = requirement.get("requested_time")
    available = list((st.slots or {}).get("available") or [])

    if run_status == "incomplete":
        outcome.task_status = "incomplete"
        outcome.status_reason = run_reason or "达到执行上限，任务未完成。"
    elif run_status == "failed":
        outcome.task_status = "failed"
        outcome.status_reason = run_reason or "运行失败。"
    elif draft:
        deduplicated = bool(draft.get("deduplicated"))
        if draft.get("confirmation_status") == "pending_confirmation":
            outcome.task_status = "completed_pending_confirmation"
            outcome.status_reason = "草稿 %s 已保存，待用户确认后发布。" % outcome.draft_id
        else:
            outcome.task_status = "completed"
            outcome.status_reason = "草稿 %s 已保存，无需确认。" % outcome.draft_id
        if deduplicated:
            outcome.status_reason = "相同请求命中已有草稿 %s（幂等），未重复创建。" % outcome.draft_id
    elif outcome.review_status == "needs_input":
        outcome.task_status = "needs_input"
        outcome.status_reason = "素材信息缺失：%s。" % ", ".join(st.missing_fields)
    elif st.rules and st.rules.get("blocked"):
        blocking = [f["rule_id"] for f in st.rules.get("findings", []) if f.get("severity") == "blocking"]
        outcome.task_status = "blocked"
        outcome.status_reason = "存在阻断级审核风险：%s。" % ", ".join(blocking)
    elif st.last_save_error:
        error = st.last_save_error
        code = error.get("code")
        if code == "HARD_REQUIREMENT_CHANGE_BLOCKED":
            outcome.task_status = "blocked"
            outcome.status_reason = "硬性发布要求不得擅自修改：%s。" % error.get("message")
        elif code == "CHANNEL_NOT_ALLOWED":
            outcome.task_status = "blocked"
            outcome.status_reason = "发布渠道不得擅自修改：%s。" % error.get("message")
        elif code == "INCOMPLETE_ASSET":
            outcome.task_status = "needs_input"
            outcome.status_reason = "素材信息缺失：%s。" % error.get("message")
        elif code == "RULE_BLOCKING":
            outcome.task_status = "blocked"
            outcome.status_reason = "存在阻断级审核风险：%s。" % error.get("message")
        else:
            outcome.task_status = "failed"
            outcome.status_reason = "草稿保存失败：%s — %s" % (code, error.get("message"))
    elif st.tool_errors and st.asset is None:
        outcome.task_status = "failed"
        outcome.status_reason = "读取素材失败：%s。" % st.tool_errors[-1].get("message", "未知错误")
    elif st.slot_error:
        outcome.task_status = "failed"
        outcome.status_reason = "时段查询失败：%s — %s" % (
            st.slot_error.get("code"),
            st.slot_error.get("message"),
        )
    elif st.slots is not None and not available:
        outcome.task_status = "blocked"
        outcome.status_reason = "当天无可用发布时段，需用户确认更换日期或放宽时段。"
    elif st.slots is not None and flexibility == "fixed" and requested_time not in available:
        outcome.task_status = "blocked"
        outcome.status_reason = "硬性发布要求 %s 已被占用，未经用户同意不得改期。" % requested_time
    else:
        outcome.task_status = "incomplete"
        outcome.status_reason = run_reason or "未完成排期。"

    if not outcome.pending_confirmations:
        outcome.pending_confirmations = build_pending_confirmations(st, outcome)
    return outcome


def build_pending_confirmations(st: AssetState, outcome: AssetOutcome) -> List[str]:
    questions: List[str] = []
    if outcome.review_status == "needs_input":
        questions.append("请补充素材信息：%s。" % ", ".join(st.missing_fields or []))
        return questions
    if outcome.review_status == "blocked" and st.rules:
        for finding in st.rules.get("findings", []):
            if finding.get("severity") == "blocking":
                questions.append(
                    "请先处理阻断风险 %s（%s）：%s；处理后再排期。"
                    % (
                        finding.get("rule_id"),
                        finding.get("name"),
                        describe_evidence(finding),
                    )
                )
        return questions
    if outcome.draft_status == "failed" and st.last_save_error:
        error = st.last_save_error
        code = error.get("code")
        details = error.get("details") or {}
        if code == "HARD_REQUIREMENT_CHANGE_BLOCKED":
            questions.append(
                "硬性要求 %s 已被占用：请确认是否调整硬性发布要求（需业务方确认），或本期不排期？"
                % details.get("requested_time", "")
            )
        elif code == "INCOMPLETE_ASSET":
            questions.append("请补充素材信息：%s。" % ", ".join(details.get("missing_fields") or []))
        elif code == "RULE_BLOCKING":
            for finding in details.get("blocking_findings") or []:
                questions.append(
                    "请先处理阻断风险 %s（%s）。" % (finding.get("rule_id"), finding.get("name"))
                )
        else:
            questions.append("草稿未保存（%s）：%s 请确认是否重试。" % (code, error.get("message")))
        return questions
    if st.slot_error:
        questions.append("时段服务暂不可用，待恢复后重新排期。")
        return questions
    requirement = (st.asset or {}).get("publish_requirement") or {}
    if st.slots is not None and not st.slots.get("available"):
        questions.append("当天无可用时段：请确认是否更换发布日期或放宽时段？")
        return questions
    if (
        st.slots is not None
        and requirement.get("flexibility") == "fixed"
        and requirement.get("requested_time") not in (st.slots.get("available") or [])
    ):
        questions.append(
            "硬性要求 %s 已被占用：请确认是否调整硬性发布要求（需业务方确认），或本期不排期？"
            % requirement.get("requested_time")
        )
        return questions
    if outcome.review_status == "needs_confirmation" and st.rules:
        for finding in st.rules.get("findings", []):
            if finding.get("severity") == "warn":
                questions.append(
                    "%s（%s）：%s；需人工确认。"
                    % (finding.get("name"), finding.get("rule_id"), describe_evidence(finding))
                )
    return questions


def propose_slot(st: AssetState) -> Optional[Dict[str, Any]]:
    if not st.slots:
        return None
    asset = st.asset or {}
    requirement = asset.get("publish_requirement") or {}
    flexibility = requirement.get("flexibility")
    requested = requirement.get("requested_time")
    available = list(st.slots.get("available") or [])
    date = st.slots.get("date") or (asset.get("publish_window") or {}).get("date")
    channel = st.slots.get("channel") or requirement.get("channel")

    chosen: Optional[str] = None
    if flexibility == "open":
        chosen = available[0] if available else None
    elif flexibility == "fixed":
        chosen = requested if requested in available else None
    elif requested in available:
        chosen = requested
    elif available:
        later = [slot for slot in available if requested and slot > requested]
        chosen = later[0] if later else available[-1]

    if not chosen:
        return None
    return {"date": date, "time": chosen, "channel": channel}
