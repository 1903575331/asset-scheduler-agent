"""Offline deterministic "policy model" used when no LLM credential is configured.

IMPORTANT BOUNDARY (also documented in README/docs/03-evaluation.md):
this is a *mock* decision maker.  It observes the same tool results an LLM would
see and selects among multiple actions (read asset / check rules / query slots /
save / re-query / retry / stop / request confirmation), so it exercises the whole
runtime contract deterministically.  It does NOT demonstrate real-LLM decision
quality, and its behaviour is not evidence that a real model would decide well.
The real model path lives in ``llm.py`` and is exercised through the same loop.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import AgentState, AssetState, Decision


def tool(name: str, reason: str, **args: Any) -> Decision:
    return Decision(action="tool", reason=reason, tool=name, args=args)


def finish(reason: str) -> Decision:
    return Decision(action="finish", reason=reason)


class OfflinePolicyMock:
    """Deterministic ReAct-style policy over tool observations."""

    name = "offline-policy-mock"

    # ------------------------------------------------------------------ decide
    def decide(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], state: AgentState) -> Decision:
        request = state.request

        if request.mode == "pending":
            if state.candidates is None:
                return tool(
                    "list_pending_assets",
                    "用户要求处理待审素材，先列出清单以确定处理范围。",
                    limit=request.limit,
                )
            if not state.candidates:
                return finish("没有待审核素材，无需排期。")
            ids = ", ".join(a.get("asset_id", "") for a in state.candidates)
            return finish("已发现待处理素材（%s），交由调用方逐项排期。" % ids)

        asset_id = request.asset_ids[0]
        st = state.ensure_asset(asset_id)
        return self._decide_asset(st)

    # ------------------------------------------------------------------- steps
    def _decide_asset(self, st: AssetState) -> Decision:
        if st.asset is None:
            if st.tool_errors:
                code = st.tool_errors[-1].get("code", "UNKNOWN")
                return finish("读取素材失败（%s），无法继续排期，未保存草稿。" % code)
            return tool("get_asset", "尚未读取素材 %s 的元数据与发布要求，先补全事实。" % st.asset_id, asset_id=st.asset_id)

        if st.missing_fields:
            return finish(
                "素材 %s 缺少必要字段（%s），无法安全生成排期，需先补充信息。"
                % (st.asset_id, ", ".join(st.missing_fields))
            )

        if st.rules is None:
            return tool("check_review_rules", "排期前先按规则检查风险。", asset_id=st.asset_id)

        if st.rules.get("blocked"):
            blocking = [f["rule_id"] for f in st.rules.get("findings", []) if f.get("severity") == "blocking"]
            return finish(
                "素材 %s 存在阻断级审核风险（%s），不保存草稿，需先处理风险。"
                % (st.asset_id, ", ".join(blocking))
            )

        if st.slots is None:
            if st.slot_error:
                return finish(
                    "时段查询失败（%s），无法确认可用时段，未保存草稿。"
                    % st.slot_error.get("code", "UNKNOWN")
                )
            asset = st.asset or {}
            requirement = asset.get("publish_requirement") or {}
            date = (asset.get("publish_window") or {}).get("date")
            return tool(
                "query_publish_slots",
                "需要确认 %s %s 的实际占用情况，不能凭要求时间直接排期。"
                % (date, requirement.get("channel")),
                date=date,
                channel=requirement.get("channel"),
                asset_id=st.asset_id,
            )

        if st.draft is None:
            if st.last_save_error:
                return self._handle_save_error(st)
            return self._plan_slot(st)

        if not st.draft_verified:
            return tool(
                "get_schedule_draft",
                "草稿已保存，读取一次以核验状态与确认标记。",
                draft_id=st.draft.get("draft_id"),
            )

        confirmation = st.draft.get("confirmation_status")
        if confirmation == "pending_confirmation":
            questions = "；".join(st.draft.get("open_questions") or [])
            return finish(
                "草稿 %s 已保存，但存在待确认项（%s），未发布，等待用户确认。"
                % (st.draft.get("draft_id"), questions or "需人工确认")
            )
        return finish("草稿 %s 已保存，无待确认项，未发布。" % st.draft.get("draft_id"))

    # ------------------------------------------------------------------ saving
    def _plan_slot(self, st: AssetState) -> Decision:
        asset = st.asset or {}
        requirement = asset.get("publish_requirement") or {}
        flexibility = requirement.get("flexibility")
        requested = requirement.get("requested_time")
        date = (st.slots or {}).get("date") or (asset.get("publish_window") or {}).get("date")
        channel = (st.slots or {}).get("channel") or requirement.get("channel")
        available = list((st.slots or {}).get("available") or [])

        if flexibility == "open":
            if not available:
                return finish("素材 %s 当天无可用时段，无法排期，需用户确认更换日期。" % st.asset_id)
            chosen = available[0]
            return tool(
                "save_schedule_draft",
                "用户未指定发布时间，选择当天最早可用时段 %s。" % chosen,
                asset_id=st.asset_id,
                date=date,
                time=chosen,
                channel=channel,
                decision_note="open 要求：自动选择最早可用时段 %s。" % chosen,
                open_questions=[],
            )

        if requested in available:
            note = "请求时段 %s 空闲，按用户要求保存草稿。" % requested
            return tool(
                "save_schedule_draft",
                note,
                asset_id=st.asset_id,
                date=date,
                time=requested,
                channel=channel,
                decision_note=note,
                open_questions=[],
            )

        if flexibility == "fixed":
            occupied_by = self._occupier(st, requested)
            return finish(
                "硬性发布要求 %s 已被占用（%s），未经用户同意不得改期，需用户决定。"
                % (requested, occupied_by or "已有排期")
            )

        # preferred: suggest a nearby free slot, still pending confirmation.
        candidate = self._nearest_available(available, requested)
        if candidate is None:
            return finish("素材 %s 当天无可用时段，需用户确认更换日期或放宽时段。" % st.asset_id)
        questions = ["原要求 %s 已被占用，是否同意改为 %s？" % (requested, candidate)]
        note = "请求时段 %s 被占用，改用最近的可用时段 %s，标记待确认。" % (requested, candidate)
        return tool(
            "save_schedule_draft",
            note,
            asset_id=st.asset_id,
            date=date,
            time=candidate,
            channel=channel,
            decision_note=note,
            open_questions=questions,
        )

    def _handle_save_error(self, st: AssetState) -> Decision:
        error = st.last_save_error or {}
        code = error.get("code", "UNKNOWN")
        retryable = bool(error.get("retryable"))

        if retryable and st.save_attempts < 2:
            return self._plan_slot_retry(st)

        if code == "HARD_REQUIREMENT_CHANGE_BLOCKED":
            return finish("工具拒绝变更硬性发布要求，需用户决定，未保存草稿。")
        if code == "RULE_BLOCKING":
            return finish("工具因阻断级审核风险拒绝保存草稿，需先处理风险。")
        if code == "INCOMPLETE_ASSET":
            return finish("素材信息不完整，无法保存草稿，需补充信息。")
        if code == "SLOT_CONFLICT":
            return finish("保存时目标时段已被占用，未保存草稿，需重新排期。")
        return finish("草稿保存失败（%s：%s），按实际结果报告失败。" % (code, error.get("message", "")))

    def _plan_slot_retry(self, st: AssetState) -> Decision:
        asset = st.asset or {}
        requirement = asset.get("publish_requirement") or {}
        requested = requirement.get("requested_time")
        date = (asset.get("publish_window") or {}).get("date")
        channel = requirement.get("channel")
        available = list((st.slots or {}).get("available") or [])
        flexibility = requirement.get("flexibility")

        if flexibility == "open" and available:
            chosen = available[0]
        elif requested in available:
            chosen = requested
        else:
            chosen = self._nearest_available(available, requested)
        if chosen is None:
            return finish("草稿保存失败且无可用时段，未能完成排期。")

        questions: List[str] = []
        if flexibility == "preferred" and requested and chosen != requested:
            questions = ["原要求 %s 已被占用，是否同意改为 %s？" % (requested, chosen)]
        note = "上次保存失败，重试保存草稿（%s %s）。" % (date, chosen)
        return tool(
            "save_schedule_draft",
            "保存失败属于可重试的存储错误，重试一次；若仍失败则如实报告。",
            asset_id=st.asset_id,
            date=date,
            time=chosen,
            channel=channel,
            decision_note=note,
            open_questions=questions,
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _nearest_available(available: List[str], requested: Optional[str]) -> Optional[str]:
        if not available:
            return None
        if not requested:
            return available[0]
        later = [slot for slot in available if slot > requested]
        if later:
            return later[0]
        return available[-1]

    @staticmethod
    def _occupier(st: AssetState, requested: Optional[str]) -> str:
        for slot in (st.slots or {}).get("slots", []):
            if slot.get("time") == requested and slot.get("status") == "occupied":
                occupied_by = slot.get("occupied_by") or {}
                return "%s（%s）" % (occupied_by.get("title") or "已有排期", occupied_by.get("asset_id") or "未知素材")
        return ""
