#!/usr/bin/env python3
"""Verify a signed TRACE record (thin wrapper around ``lib.verify_trace``)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.verify_trace import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
