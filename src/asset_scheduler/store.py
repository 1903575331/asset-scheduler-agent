"""Data access layer: assets, rules, existing schedule, draft store and faults.

Persistence is a JSON file so the reviewer can inspect the exact state without
starting a database.  All writes are atomic (tmp file + os.replace) and every
draft save is idempotent by a deterministic key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .domain import describe_evidence

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class RejectedAction(Exception):
    """A business/safety rule rejected the action; tools translate it to ToolResult."""

    def __init__(self, code: str, message: str, retryable: bool = False, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class DataRepository:
    """Loads static data once and owns the mutable draft store."""

    def __init__(self, data_dir: Path, state_dir: Path):
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        assets_doc = load_json(self.data_dir / "assets.json")
        self.assets: Dict[str, Dict[str, Any]] = {a["asset_id"]: a for a in assets_doc["assets"]}
        self.rules: List[Dict[str, Any]] = load_json(self.data_dir / "rules.json")["rules"]
        self.schedule: Dict[str, Any] = load_json(self.data_dir / "schedule.json")

        faults_path = self.data_dir / "faults.json"
        if faults_path.is_file():
            self.faults: Dict[str, Any] = load_json(faults_path).get("faults", {})
        else:
            self.faults = {}

        self.drafts_path = self.state_dir / "drafts.json"
        if self.drafts_path.is_file():
            doc = load_json(self.drafts_path)
        else:
            doc = {"drafts": {}, "meta": {"fault_attempts": {}}}
        doc.setdefault("drafts", {})
        doc.setdefault("meta", {}).setdefault("fault_attempts", {})
        self._doc: Dict[str, Any] = doc

    # ------------------------------------------------------------------ assets
    def asset(self, asset_id: str) -> Dict[str, Any]:
        try:
            return copy.deepcopy(self.assets[asset_id])
        except KeyError:
            raise RejectedAction("ASSET_NOT_FOUND", "素材不存在：%s" % asset_id, details={"asset_id": asset_id})

    def has_asset(self, asset_id: str) -> bool:
        return asset_id in self.assets

    def pending_assets(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = []
        for asset_id in sorted(self.assets):
            asset = self.assets[asset_id]
            if asset.get("status") != "pending_review":
                continue
            rows.append(
                {
                    "asset_id": asset_id,
                    "title": asset.get("title"),
                    "owner": asset.get("owner"),
                    "asset_type": asset.get("asset_type"),
                    "publish_window": asset.get("publish_window") or {},
                }
            )
        return rows[: max(1, int(limit))]

    @staticmethod
    def missing_fields(asset: Dict[str, Any]) -> List[str]:
        """Fields that are required before a draft can be saved."""
        missing: List[str] = []
        if not asset.get("title"):
            missing.append("title")
        if not asset.get("owner"):
            missing.append("owner")
        if not asset.get("asset_type"):
            missing.append("asset_type")
        if not asset.get("aspect_ratio"):
            missing.append("aspect_ratio")
        if asset.get("asset_type") == "video" and not asset.get("duration_sec"):
            missing.append("duration_sec")
        if not (asset.get("publish_window") or {}).get("date"):
            missing.append("publish_window.date")
        requirement = asset.get("publish_requirement") or {}
        if not requirement.get("channel"):
            missing.append("publish_requirement.channel")
        flexibility = requirement.get("flexibility")
        if flexibility not in {"fixed", "preferred", "open"}:
            missing.append("publish_requirement.flexibility")
        elif flexibility in {"fixed", "preferred"} and not requirement.get("requested_time"):
            missing.append("publish_requirement.requested_time")
        return missing

    # ------------------------------------------------------------------- rules
    def check_rules(self, asset: Dict[str, Any]) -> Dict[str, Any]:
        rule_book = {r["rule_id"]: r for r in self.rules}
        findings: List[Dict[str, Any]] = []

        def add(rule_id: str, evidence: Any) -> None:
            rule = rule_book.get(rule_id, {})
            findings.append(
                {
                    "rule_id": rule_id,
                    "name": rule.get("name", rule_id),
                    "severity": rule.get("severity", "blocking"),
                    "message": rule.get("description", ""),
                    "evidence": evidence,
                }
            )

        missing = self.missing_fields(asset)
        if missing:
            add("R-MISS-01", {"missing_fields": missing})

        review = asset.get("review") or {}
        if review.get("copyright_cleared") is not True:
            add("R-COPY-01", {"copyright_cleared": review.get("copyright_cleared")})
        if review.get("music_licensed") is not True:
            add("R-MUSIC-01", {"music_licensed": review.get("music_licensed")})

        sensitive = review.get("sensitive_words") or []
        if sensitive:
            add("R-SENS-01", {"matched_words": list(sensitive)})

        if asset.get("asset_type") == "video" and asset.get("aspect_ratio") != "9:16":
            add("R-RATIO-01", {"aspect_ratio": asset.get("aspect_ratio")})

        blocked = any(f["severity"] == "blocking" for f in findings)
        needs_confirmation = any(f["severity"] == "warn" for f in findings)
        return {
            "asset_id": asset["asset_id"],
            "findings": findings,
            "blocked": blocked,
            "needs_confirmation": needs_confirmation,
        }

    # ------------------------------------------------------------------- slots
    def _draft_bookings(self, date: str, channel: str, exclude_asset_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        booked: Dict[str, Dict[str, Any]] = {}
        for draft in self._doc["drafts"].values():
            if draft.get("status") != "draft":
                continue
            if exclude_asset_id and draft.get("asset_id") == exclude_asset_id:
                # An asset's own draft is not a competitor for a re-plan of the
                # same asset; it is superseded when the new draft is saved.
                continue
            if draft.get("date") == date and draft.get("channel") == channel:
                booked[draft["time"]] = {
                    "asset_id": draft.get("asset_id"),
                    "title": "草稿：" + str(draft.get("asset_id")),
                    "draft_id": draft.get("draft_id"),
                }
        return booked

    def list_slots(self, date: str, channel: str, exclude_asset_id: Optional[str] = None) -> Dict[str, Any]:
        if not date or not channel:
            raise RejectedAction("INVALID_ARGS", "date 与 channel 均不能为空。")
        booked: Dict[str, Dict[str, Any]] = self._draft_bookings(date, channel, exclude_asset_id=exclude_asset_id)
        for booking in self.schedule.get("bookings", []):
            if booking.get("date") == date and booking.get("channel") == channel:
                booked[booking["time"]] = {
                    "asset_id": booking.get("asset_id"),
                    "title": booking.get("title", ""),
                    "draft_id": None,
                }

        slots: List[Dict[str, Any]] = []
        available: List[str] = []
        for time, config in self.schedule.get("slots", {}).items():
            if not config.get("enabled", True):
                continue
            if time in booked:
                slots.append({"time": time, "status": "occupied", "occupied_by": booked[time]})
            else:
                slots.append({"time": time, "status": "free", "occupied_by": None})
                available.append(time)
        return {"date": date, "channel": channel, "slots": slots, "available": available}

    # ------------------------------------------------------------------ drafts
    @staticmethod
    def _request_signature(asset: Dict[str, Any], date: str, channel: str) -> str:
        requirement = asset.get("publish_requirement") or {}
        raw = "|".join(
            [
                str(asset.get("asset_id")),
                str(date),
                str(channel),
                str(requirement.get("requested_time") or ""),
                str(requirement.get("flexibility") or ""),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _draft_key(signature: str, time: str) -> str:
        return hashlib.sha1(("%s|%s" % (signature, time)).encode("utf-8")).hexdigest()

    def find_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        draft = self._doc["drafts"].get(draft_id)
        return copy.deepcopy(draft) if draft else None

    def drafts_for_asset(self, asset_id: str) -> List[Dict[str, Any]]:
        return [copy.deepcopy(d) for d in self._doc["drafts"].values() if d.get("asset_id") == asset_id]

    def save_draft(
        self,
        asset_id: str,
        date: str,
        time: str,
        channel: str,
        decision_note: str = "",
        open_questions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Validate, deduplicate, persist and return a draft record.

        Safety guardrails live here (not only in the prompt): a model cannot
        silently change a fixed requirement, cannot bypass a blocking rule and
        can never mark anything as published.
        """
        asset = self.asset(asset_id)
        missing = self.missing_fields(asset)
        if missing:
            raise RejectedAction(
                "INCOMPLETE_ASSET",
                "素材信息缺失，无法保存草稿：%s" % ", ".join(missing),
                details={"missing_fields": missing},
            )

        rule_report = self.check_rules(asset)
        if rule_report["blocked"]:
            blocking = [f for f in rule_report["findings"] if f["severity"] == "blocking"]
            raise RejectedAction(
                "RULE_BLOCKING",
                "存在阻断级审核风险，不能保存草稿。",
                details={"blocking_findings": blocking},
            )

        requirement = asset.get("publish_requirement") or {}
        requested_time = requirement.get("requested_time")
        flexibility = requirement.get("flexibility")
        window_date = (asset.get("publish_window") or {}).get("date")

        if not date or not channel:
            raise RejectedAction("INVALID_ARGS", "date 与 channel 均不能为空。")
        if not time or not TIME_RE.match(str(time)):
            raise RejectedAction("INVALID_ARGS", "time 必须为 HH:MM 格式。", details={"time": time})
        if date != window_date:
            raise RejectedAction(
                "DATE_OUT_OF_WINDOW",
                "发布时间窗为 %s，不能改到 %s（硬性发布要求不得擅自变更）。" % (window_date, date),
                details={"publish_window": window_date, "requested_date": date},
            )
        if requirement.get("channel") and channel != requirement.get("channel"):
            raise RejectedAction(
                "CHANNEL_NOT_ALLOWED",
                "发布渠道为 %s，不能改到 %s（硬性发布要求不得擅自变更）。"
                % (requirement.get("channel"), channel),
                details={"requested_channel": requirement.get("channel"), "proposed_channel": channel},
            )
        if str(time) not in self.schedule.get("slots", {}):
            raise RejectedAction("INVALID_ARGS", "时段不存在：%s" % time)

        if flexibility == "fixed" and requested_time and time != requested_time:
            raise RejectedAction(
                "HARD_REQUIREMENT_CHANGE_BLOCKED",
                "硬性发布要求为 %s，未经用户同意不得改到 %s。" % (requested_time, time),
                details={"requested_time": requested_time, "proposed_time": time},
            )

        # Slot availability (drafts + confirmed schedule).  The asset's own
        # previous drafts are excluded because a new draft for it replaces them.
        availability = self.list_slots(date, channel, exclude_asset_id=asset_id)
        slot = next((s for s in availability["slots"] if s["time"] == time), None)
        if slot is None:
            raise RejectedAction("INVALID_ARGS", "时段不存在：%s" % time)
        if slot["status"] == "occupied":
            raise RejectedAction(
                "SLOT_CONFLICT",
                "%s %s 已被占用。" % (date, time),
                retryable=False,
                details={"occupied_by": slot.get("occupied_by")},
            )

        changed = bool(requested_time and time != requested_time)
        warn_findings = [f for f in rule_report["findings"] if f["severity"] == "warn"]
        confirmation_status = "not_required"
        if changed and flexibility != "fixed":
            confirmation_status = "pending_confirmation"
        if warn_findings:
            confirmation_status = "pending_confirmation"

        questions = list(open_questions or [])
        if changed and flexibility == "preferred":
            default_question = "原要求 %s 已被占用，是否同意改为 %s？" % (requested_time, time)
            if default_question not in questions:
                questions.insert(0, default_question)
        for finding in warn_findings:
            question = "%s（%s）：%s；需人工确认后发布。" % (
                finding["name"],
                finding["rule_id"],
                describe_evidence(finding),
            )
            if question not in questions:
                questions.append(question)

        signature = self._request_signature(asset, date, channel)
        draft_key = self._draft_key(signature, time)
        draft_id = "draft-" + draft_key[:10]

        existing = self._doc["drafts"].get(draft_id)
        if existing and existing.get("status") == "draft":
            payload = copy.deepcopy(existing)
            payload["deduplicated"] = True
            payload["saved"] = True
            return payload

        # Fault injection happens before any state mutation so a failed save
        # never leaves a half-written supersede trail behind.
        self._maybe_fault(asset_id, "save")

        # A newer draft for the same asset replaces its previous active draft.
        superseded: List[str] = []
        for other_id, other in self._doc["drafts"].items():
            if other_id == draft_id:
                continue
            if other.get("status") == "draft" and other.get("asset_id") == asset_id:
                other["status"] = "superseded"
                other["superseded_by"] = draft_id
                superseded.append(other_id)

        record = {
            "draft_id": draft_id,
            "asset_id": asset_id,
            "date": date,
            "time": time,
            "channel": channel,
            "request_signature": signature,
            "flexibility": flexibility,
            "requested_time": requested_time,
            "confirmation_status": confirmation_status,
            "open_questions": questions,
            "decision_note": decision_note,
            "status": "draft",
            "published": False,
            "created_at": utc_now(),
            "supersedes": superseded,
        }
        self._doc["drafts"][draft_id] = record
        self._flush()
        payload = copy.deepcopy(record)
        payload["deduplicated"] = False
        payload["saved"] = True
        return payload

    # ------------------------------------------------------------------- faults
    def _maybe_fault(self, asset_id: str, tool: str) -> None:
        config = (self.faults.get(asset_id) or {}).get(tool)
        if not config:
            return
        key = "%s:%s" % (asset_id, tool)
        attempts = self._doc["meta"]["fault_attempts"]
        used = int(attempts.get(key, 0)) + 1
        attempts[key] = used
        self._flush()
        if used <= int(config.get("fail_times", 0)):
            raise RejectedAction(
                str(config.get("code") or "INJECTED_FAULT"),
                str(config.get("message") or "模拟故障。"),
                retryable=bool(config.get("retryable", False)),
                details={"asset_id": asset_id, "tool": tool, "attempt": used, "injected": True},
            )

    def _flush(self) -> None:
        atomic_write_json(self.drafts_path, self._doc)

    def reset_state(self) -> None:
        self._doc = {"drafts": {}, "meta": {"fault_attempts": {}}}
        self._flush()


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
