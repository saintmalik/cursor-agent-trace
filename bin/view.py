#!/usr/bin/env python3
"""Thin wrapper: ``python bin/view.py trail.jsonl`` → ``lib.view``."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.view import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
