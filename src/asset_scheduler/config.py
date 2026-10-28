"""Configuration loading (env vars + optional local .env file).

No third-party dependency is used; the .env parser only supports the subset we
need (KEY=VALUE, optional quotes, '#' comments, blank lines).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from ``path`` into os.environ (never overriding)."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Effective runtime settings."""

    project_root: Path
    data_dir: Path
    state_dir: Path
    trace_dir: Optional[Path]
    model_provider: str  # auto | llm | mock
    api_key: Optional[str]
    base_url: str
    model_name: str
    temperature: float
    max_steps: int
    max_tool_calls: int
    timeout_sec: float

    @classmethod
    def from_env(
        cls,
        project_root: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        trace_dir: Optional[Path] = None,
        model_provider: Optional[str] = None,
        max_steps: Optional[int] = None,
        max_tool_calls: Optional[int] = None,
    ) -> "Settings":
        root = Path(project_root) if project_root else PROJECT_ROOT
        load_env_file(root / ".env")

        env_provider = (model_provider or os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
        if env_provider not in {"auto", "llm", "mock"}:
            env_provider = "auto"

        state = Path(state_dir) if state_dir else Path(os.environ.get("AGENT_STATE_DIR", str(root / ".runtime")))
        if not state.is_absolute():
            state = root / state
        trace = Path(trace_dir) if trace_dir else None
        if trace is not None and not trace.is_absolute():
            trace = root / trace

        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or None
        return cls(
            project_root=root,
            data_dir=root / "data",
            state_dir=state,
            trace_dir=trace,
            model_provider=env_provider,
            api_key=api_key,
            base_url=(os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/"),
            model_name=os.environ.get("OPENAI_MODEL") or "deepseek-chat",
            temperature=_float_env("OPENAI_TEMPERATURE", 0.0),
            max_steps=max_steps if max_steps is not None else _int_env("AGENT_MAX_STEPS", 14),
            max_tool_calls=max_tool_calls if max_tool_calls is not None else _int_env("AGENT_MAX_TOOL_CALLS", 16),
            timeout_sec=_float_env("AGENT_TIMEOUT_SEC", 60.0),
        )
