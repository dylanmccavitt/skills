#!/usr/bin/env python3
"""Compatibility entry point for the shared orchestration hook."""

from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from orchestration_hook import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
