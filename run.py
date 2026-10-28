#!/usr/bin/env python
"""Convenience launcher: ``python run.py ...`` from the repository root."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.asset_scheduler.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
