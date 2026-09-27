#!/usr/bin/env python3
"""Thin wrapper: ``python bin/doctor.py`` → ``lib.doctor``."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.doctor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
