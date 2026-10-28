"""Domain types shared by the runtime, the tools and both model backends.

The structures here are intentionally provider-agnostic: the offline policy
mock and the real LLM adapter both return ``Decision`` objects and both see the
same ``AgentState``.  The final answer is always derived from tool observations
(see ``agent.build_outcome``), never from a model's claim.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utcnow_iso() -> str:
    """Timestamp used in run records (UTC, second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def describe_evidence(finding: Dict[str, Any]) -> str:
    """Short human-readable evidence for a rule finding (used in confirmations)."""
    evidence = finding.get("evidence") or {}
    if not isinstance(evidence, dict):
        return str(evidence)
    if "matched_words" in evidence:
        words = evidence.get("matched_words") or []
        return "命中词：%s" % "、".join(str(w) for w in words)
    if "missing_fields" in evidence:
        fields = evidence.get("missing_fields") or []
        return "缺失字段：%s" % ", ".join(str(f) for f in fields)
    if "aspect_ratio" in evidence:
        return "当前画幅：%s" % evidence.get("aspect_ratio")
    if "copyright_cleared" in evidence:
        return "copyright_cleared=%s" % evidence.get("copyright_cleared")
    if "music_licensed" in evidence:
        return "music_licensed=%s" % evidence.get("music_licensed")
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True)


def _clean(value: Any) -> Any:
    """Convert dataclasses / nested containers into JSON-serialisable values."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


@dataclass
class Request:
    """One user request handed to the agent.

    ``mode`` is either ``asset`` (schedule the listed assets) or ``pending``
    (first discover pending assets, then let the caller schedule them).
    """

    request_id: str
    asset_ids: List[str] = field(default_factory=list)
    user_message: str = ""
    mode: str = "asset"
    limit: int = 10
    source: str = "cli"

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], source: str = "case") -> "Request":
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
        request_id = str(payload.get("request_id") or "request")
        asset_ids = payload.get("asset_ids") or []
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        asset_ids = [str(a) for a in asset_ids]
        mode = str(payload.get("mode") or ("pending" if not asset_ids else "asset")).lower()
        if mode not in {"asset", "pending"}:
            raise ValueError("request.mode must be 'asset' or 'pending'")
        if mode == "asset" and not asset_ids:
            raise ValueError("request.asset_ids is required when mode='asset'")
        return cls(
            request_id=request_id,
            asset_ids=asset_ids,
            user_message=str(payload.get("user_message") or ""),
            mode=mode,
            limit=int(payload.get("limit") or 10),
            source=source,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "asset_ids": list(self.asset_ids),
            "user_message": self.user_message,
            "mode": self.mode,
            "limit": self.limit,
            "source": self.source,
        }


@dataclass
class Decision:
    """What the model wants to do next.

    ``action`` is ``tool`` (call exactly one tool) or ``finish`` (stop and let
    the runtime compute the authoritative result).  ``reason`` is a brief
    decision rationale that is safe to publish in traces (no hidden CoT).
    """

    action: str
    reason: str
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "tool": self.tool, "args": self.args}


@dataclass
class ToolError:
    code: str
    message: str
    retryable: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


@dataclass
class ToolResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[ToolError] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "data": _clean(self.data), "error": self.error.to_dict() if self.error else None}


@dataclass
class StepRecord:
    """One model decision + (optional) tool observation in a trace."""

    index: int
    reason: str
    action: str
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    result_ok: Optional[bool] = None
    result_summary: str = ""
    error_code: Optional[str] = None
    duration_ms: int = 0
    next_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AssetState:
    """Working memory for one asset inside a run (built from tool results)."""

    asset_id: str
    asset: Optional[Dict[str, Any]] = None
    missing_fields: List[str] = field(default_factory=list)
    rules: Optional[Dict[str, Any]] = None
    slots: Optional[Dict[str, Any]] = None
    draft: Optional[Dict[str, Any]] = None
    draft_verified: bool = False
    save_attempts: int = 0
    last_save_error: Optional[Dict[str, Any]] = None
    slot_error: Optional[Dict[str, Any]] = None
    tool_errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "missing_fields": list(self.missing_fields),
            "rules_checked": self.rules is not None,
            "slots_checked": self.slots is not None,
            "draft_id": (self.draft or {}).get("draft_id"),
            "draft_verified": self.draft_verified,
            "save_attempts": self.save_attempts,
            "last_save_error": self.last_save_error,
            "slot_error": self.slot_error,
            "tool_errors": list(self.tool_errors),
        }


@dataclass
class AgentState:
    """Mutable state of a single agent run (one asset, or one discovery run)."""

    request: Request
    assets: Dict[str, AssetState] = field(default_factory=dict)
    candidates: Optional[List[Dict[str, Any]]] = None
    final_reason: str = ""
    limit_hit: Optional[str] = None

    def ensure_asset(self, asset_id: str) -> AssetState:
        key = str(asset_id)
        if key not in self.assets:
            self.assets[key] = AssetState(asset_id=key)
        return self.assets[key]

    @property
    def current_asset_id(self) -> str:
        if self.request.asset_ids:
            return self.request.asset_ids[0]
        return ""


@dataclass
class AssetOutcome:
    """Reviewer-facing result for one asset (derived from observations)."""

    asset_id: str
    title: Optional[str] = None
    review_status: str = "unverified"
    risk_reasons: List[Dict[str, Any]] = field(default_factory=list)
    suggested_slot: Optional[Dict[str, Any]] = None
    pending_confirmations: List[str] = field(default_factory=list)
    draft_status: str = "not_attempted"
    draft_id: Optional[str] = None
    draft_error: Optional[Dict[str, Any]] = None
    task_status: str = "incomplete"
    status_reason: str = ""
    published: bool = False
    steps_used: int = 0
    tool_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return _clean(asdict(self))


@dataclass
class RunResult:
    """Result of one agent run (may be a discovery run or a scheduling run)."""

    run_id: str
    request: Request
    model: str
    started_at: str
    finished_at: str
    duration_ms: int
    status: str
    status_reason: str
    limits: Dict[str, Any] = field(default_factory=dict)
    steps: List[StepRecord] = field(default_factory=list)
    assets: List[AssetOutcome] = field(default_factory=list)
    discovered_assets: List[str] = field(default_factory=list)
    published: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request": self.request.to_dict(),
            "model": self.model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "status_reason": self.status_reason,
            "limits": _clean(self.limits),
            "steps": [s.to_dict() for s in self.steps],
            "assets": [a.to_dict() for a in self.assets],
            "discovered_assets": list(self.discovered_assets),
            "published": False,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
