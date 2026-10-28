"""Command line entry point.

Primary usage (from the repository root):

    python run.py run --case examples/cases/01-normal-slot-free.json
    python run.py run --pending
    python run.py run --asset M01 --model mock
    python run.py list-pending
    python run.py doctor

Also runnable as ``python -m src.asset_scheduler.cli ...``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROJECT_ROOT, Settings
from .domain import Request
from .llm import ModelError, build_model
from .runner import CaseError, execute_case, load_case, normalize_case, write_traces
from .store import DataRepository
from .trace import format_slot

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_INCOMPLETE = 3


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings.from_env(
        project_root=PROJECT_ROOT,
        state_dir=args.state_dir,
        trace_dir=args.trace_dir,
        model_provider=args.model,
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
    )


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if args.fresh:
        drafts = settings.state_dir / "drafts.json"
        if drafts.is_file():
            drafts.unlink()

    try:
        if args.case:
            case = normalize_case(load_case(Path(args.case)))
        elif args.pending:
            requests = [
                Request(
                    request_id="cli-pending",
                    mode="pending",
                    limit=max(1, args.limit),
                    user_message=args.message or "",
                    source="cli",
                )
            ]
            case = _inline_case("cli-pending", requests)
        else:
            requests = [
                Request(
                    request_id="cli-%s" % asset_id,
                    asset_ids=[asset_id],
                    user_message=args.message or "",
                    source="cli",
                )
                for asset_id in args.asset
            ]
            case = _inline_case("cli-%s" % "-".join(args.asset), requests)

        execution = execute_case(case, settings, model_override=args.model, state_dir=settings.state_dir)
    except (CaseError, ModelError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    aggregate = execution.aggregate

    trace_files: List[str] = []
    if settings.trace_dir is not None:
        written = write_traces(execution, settings.trace_dir)
        trace_files = [_rel(Path(path)) for path in written]
        aggregate["trace_files"] = trace_files

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("结果 JSON：%s" % _rel(out_path))

    _print_aggregate(aggregate)
    if trace_files:
        print("Trace 文件：%s" % ", ".join(trace_files))

    run_statuses = [run["status"] for run in aggregate["runs"]]
    if any(status == "incomplete" for status in run_statuses):
        return EXIT_INCOMPLETE
    if any(status == "failed" for status in run_statuses):
        return EXIT_CONFIG
    return EXIT_OK


def _inline_case(case_id: str, requests: List[Request]) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "title": "CLI 内联请求",
        "acceptance": [],
        "requests": requests,
        "run_options": {},
    }


def _print_aggregate(aggregate: Dict[str, Any]) -> None:
    model = aggregate.get("model") or {}
    print("")
    print("=== 运行结果 · %s ===" % (aggregate.get("case_id") or "-"))
    print("模型后端：%s" % model.get("backend"))
    if model.get("notice"):
        print("注意：%s" % model["notice"])
    run_status = aggregate["summary"].get("run_status", {})
    print("运行状态：%s" % ", ".join("%s=%d" % (k, v) for k, v in run_status.items()))

    assets = aggregate.get("assets") or []
    if not assets:
        print("未产生素材结果（可能没有待处理素材）。")
    else:
        header = "%-6s %-22s %-8s %-26s %-28s %-10s %-26s" % (
            "素材",
            "审核状态",
            "草稿",
            "建议时段",
            "待确认项",
            "任务状态",
            "说明",
        )
        print("")
        print(header)
        print("-" * 150)
        for asset in assets:
            draft = asset.get("draft_status") or "-"
            if asset.get("draft_id"):
                draft += ":%s" % asset["draft_id"]
            if asset.get("draft_error"):
                draft += "(%s)" % asset["draft_error"].get("code")
            confirmations = "；".join(asset.get("pending_confirmations") or []) or "-"
            if len(confirmations) > 30:
                confirmations = confirmations[:29] + "…"
            reason = asset.get("status_reason") or ""
            if len(reason) > 40:
                reason = reason[:39] + "…"
            print(
                "%-6s %-22s %-8s %-26s %-28s %-10s %-26s"
                % (
                    asset.get("asset_id"),
                    asset.get("review_status"),
                    draft,
                    format_slot(asset.get("suggested_slot")),
                    confirmations,
                    asset.get("task_status"),
                    reason,
                )
            )
    print("")
    print("已发布：否（系统无发布能力，所有结果均为草稿）")


def cmd_list_pending(args: argparse.Namespace) -> int:
    settings = Settings.from_env(project_root=PROJECT_ROOT, state_dir=args.state_dir, model_provider=args.model)
    repo = DataRepository(settings.data_dir, settings.state_dir)
    assets = repo.pending_assets(args.limit)
    print("待处理素材 %d 个：" % len(assets))
    for asset in assets:
        window = asset.get("publish_window") or {}
        print(
            "- %s | %s | %s | %s"
            % (asset.get("asset_id"), asset.get("title"), window.get("date"), asset.get("owner"))
        )
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = Settings.from_env(project_root=PROJECT_ROOT, model_provider=args.model)
    print("项目根目录：%s" % PROJECT_ROOT)
    print("数据目录：%s" % settings.data_dir)
    print("状态目录：%s" % settings.state_dir)
    print("Python：%s" % sys.version.split()[0])
    print("LLM_PROVIDER=%s" % settings.model_provider)
    print("OPENAI_API_KEY：%s" % ("已配置" if settings.api_key else "未配置（将使用离线 Mock）"))
    print("模型：%s @ %s" % (settings.model_name, settings.base_url))
    print("预算：max_steps=%d，max_tool_calls=%d" % (settings.max_steps, settings.max_tool_calls))
    try:
        model, notice = build_model(
            provider=settings.model_provider,
            api_key=settings.api_key,
            base_url=settings.base_url,
            model_name=settings.model_name,
            temperature=settings.temperature,
            timeout_sec=settings.timeout_sec,
        )
        print("实际使用后端：%s" % model.name)
        if notice:
            print("注意：%s" % notice)
    except ModelError as exc:
        print("模型配置错误：%s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    repo = DataRepository(settings.data_dir, settings.state_dir)
    print("素材：%d 个（待审 %d 个）" % (len(repo.assets), len(repo.pending_assets(100))))
    print("审核规则：%d 条" % len(repo.rules))
    print("已有草稿：%d 份" % len(repo._doc["drafts"]))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset-scheduler-agent",
        description="素材审核与发布排期 Agent（ReAct）· 只保存草稿，永不发布",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行一次 Agent")
    group = run.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", help="用例 JSON 文件路径")
    group.add_argument("--asset", action="append", metavar="ID", help="直接指定素材 ID（可重复）")
    group.add_argument("--pending", action="store_true", help="先发现待处理素材，再逐项排期")
    run.add_argument("--model", choices=["auto", "llm", "mock"], help="模型后端（默认读取 LLM_PROVIDER/auto）")
    run.add_argument("--state-dir", help="草稿状态目录（默认 .runtime）")
    run.add_argument("--trace-dir", help="写出 Trace 的目录")
    run.add_argument("--json-out", help="写出聚合结果 JSON 的路径")
    run.add_argument("--max-steps", type=int, help="覆盖步数上限")
    run.add_argument("--max-tool-calls", type=int, help="覆盖工具调用上限")
    run.add_argument("--limit", type=int, default=10, help="--pending 时最多发现多少个素材")
    run.add_argument("--message", default="", help="附加用户消息")
    run.add_argument("--fresh", action="store_true", help="运行前清空草稿状态（幂等演示请勿使用）")
    run.set_defaults(func=cmd_run)

    pending = sub.add_parser("list-pending", help="列出待审核素材")
    pending.add_argument("--state-dir", help="草稿状态目录")
    pending.add_argument("--model", choices=["auto", "llm", "mock"])
    pending.add_argument("--limit", type=int, default=10)
    pending.set_defaults(func=cmd_list_pending)

    doctor = sub.add_parser("doctor", help="检查环境与模型配置")
    doctor.add_argument("--model", choices=["auto", "llm", "mock"])
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
