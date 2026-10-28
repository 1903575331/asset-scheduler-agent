"""Tools exposed to the model.

Only these tools can touch data.  Their contracts carry the hard safety rules
(no publishing action exists at all; fixed requirements and blocking review
findings are rejected at the tool boundary, not only by prompt instructions).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import ToolError, ToolResult
from .store import DataRepository, RejectedAction

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "list_pending_assets",
        "description": "列出待审核素材（status=pending_review），返回素材 ID、标题、负责人和发布窗口。用于发现待处理素材。",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "最多返回多少条"}},
            "required": [],
        },
    },
    {
        "name": "get_asset",
        "description": "读取指定素材的元数据、审核信息与发布要求，并返回缺失字段列表。排期前必须先读取素材。",
        "parameters": {
            "type": "object",
            "properties": {"asset_id": {"type": "string", "description": "素材 ID，例如 M01"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "check_review_rules",
        "description": "按审核规则检查素材，返回 findings（rule_id/severity/message/evidence）、是否阻断（blocked）以及是否需要人工确认（needs_confirmation）。",
        "parameters": {
            "type": "object",
            "properties": {"asset_id": {"type": "string", "description": "素材 ID"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "query_publish_slots",
        "description": "查询某日期某渠道的发布时段占用情况，返回 slots 与 available 列表。不得凭记忆猜测时段。asset_id 为当前素材 ID（必填，用于审计与故障注入）。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "channel": {"type": "string", "description": "发布渠道，例如 抖音/视频号"},
                "asset_id": {"type": "string", "description": "当前素材 ID，例如 M01"},
            },
            "required": ["asset_id", "date", "channel"],
        },
    },
    {
        "name": "save_schedule_draft",
        "description": (
            "保存排期草稿（仅草稿，永不发布）。工具会强制校验：信息完整、无阻断风险、时段空闲、硬性要求未被擅自修改；"
            "若修改了 preferred 时段会自动标记为待确认。保存失败会返回 ok=false，必须如实报告。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "description": "素材 ID"},
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "time": {"type": "string", "description": "时段 HH:MM"},
                "channel": {"type": "string", "description": "渠道"},
                "decision_note": {"type": "string", "description": "本次排期决策说明"},
                "open_questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需要用户确认的问题列表",
                },
            },
            "required": ["asset_id", "date", "time", "channel"],
        },
    },
    {
        "name": "get_schedule_draft",
        "description": "按 draft_id 读取已保存的草稿，用于保存后核验（幂等/确认状态）。",
        "parameters": {
            "type": "object",
            "properties": {"draft_id": {"type": "string", "description": "草稿 ID"}},
            "required": ["draft_id"],
        },
    },
]


def _as_str(args: Dict[str, Any], key: str, required: bool = True) -> Optional[str]:
    value = args.get(key)
    if value is None:
        if required:
            raise RejectedAction("INVALID_ARGS", "缺少参数：%s" % key)
        return None
    value = str(value).strip()
    if required and not value:
        raise RejectedAction("INVALID_ARGS", "参数不能为空：%s" % key)
    return value


def _as_str_list(args: Dict[str, Any], key: str) -> List[str]:
    value = args.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    raise RejectedAction("INVALID_ARGS", "参数 %s 必须是字符串数组。" % key)


def _as_int(args: Dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RejectedAction("INVALID_ARGS", "参数 %s 必须是整数。" % key)


class Toolbox:
    """Dispatch table shared by both model backends."""

    def __init__(self, repo: DataRepository):
        self.repo = repo
        self._handlers = {
            "list_pending_assets": self._list_pending_assets,
            "get_asset": self._get_asset,
            "check_review_rules": self._check_review_rules,
            "query_publish_slots": self._query_publish_slots,
            "save_schedule_draft": self._save_schedule_draft,
            "get_schedule_draft": self._get_schedule_draft,
        }

    @staticmethod
    def specs() -> List[Dict[str, Any]]:
        return TOOL_SPECS

    def call(self, name: str, args: Dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                ok=False,
                error=ToolError("UNKNOWN_TOOL", "未知工具：%s" % name, retryable=False),
            )
        if not isinstance(args, dict):
            return ToolResult(
                ok=False,
                error=ToolError("INVALID_ARGS", "工具参数必须是 JSON 对象。", retryable=False),
            )
        try:
            data = handler(args)
            return ToolResult(ok=True, data=data)
        except RejectedAction as exc:
            return ToolResult(
                ok=False,
                error=ToolError(exc.code, exc.message, retryable=exc.retryable, details=exc.details),
            )
        except Exception as exc:  # defensive: tool bugs become observable errors
            return ToolResult(
                ok=False,
                error=ToolError("TOOL_INTERNAL_ERROR", "%s: %s" % (type(exc).__name__, exc), retryable=False),
            )

    # ------------------------------------------------------------- handlers
    def _list_pending_assets(self, args: Dict[str, Any]) -> Dict[str, Any]:
        limit = _as_int(args, "limit", 10)
        assets = self.repo.pending_assets(limit)
        return {"assets": assets, "count": len(assets)}

    def _get_asset(self, args: Dict[str, Any]) -> Dict[str, Any]:
        asset_id = _as_str(args, "asset_id")
        asset = self.repo.asset(asset_id)
        missing = self.repo.missing_fields(asset)
        return {"asset": asset, "missing_fields": missing}

    def _check_review_rules(self, args: Dict[str, Any]) -> Dict[str, Any]:
        asset_id = _as_str(args, "asset_id")
        asset = self.repo.asset(asset_id)
        return self.repo.check_rules(asset)

    def _query_publish_slots(self, args: Dict[str, Any]) -> Dict[str, Any]:
        asset_id = _as_str(args, "asset_id")
        date = _as_str(args, "date")
        channel = _as_str(args, "channel")
        self.repo._maybe_fault(asset_id, "slots")
        return self.repo.list_slots(date, channel, exclude_asset_id=asset_id)

    def _save_schedule_draft(self, args: Dict[str, Any]) -> Dict[str, Any]:
        asset_id = _as_str(args, "asset_id")
        date = _as_str(args, "date")
        time = _as_str(args, "time")
        channel = _as_str(args, "channel")
        decision_note = _as_str(args, "decision_note", required=False) or ""
        open_questions = _as_str_list(args, "open_questions")
        return self.repo.save_draft(
            asset_id=asset_id,
            date=date,
            time=time,
            channel=channel,
            decision_note=decision_note,
            open_questions=open_questions,
        )

    def _get_schedule_draft(self, args: Dict[str, Any]) -> Dict[str, Any]:
        draft_id = _as_str(args, "draft_id")
        draft = self.repo.find_draft(draft_id)
        if draft is None:
            raise RejectedAction("DRAFT_NOT_FOUND", "草稿不存在：%s" % draft_id, details={"draft_id": draft_id})
        return draft
