#!/usr/bin/env python3
"""Recordable demo walkthrough (thin wrapper around ``lib.demo``)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.demo import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
