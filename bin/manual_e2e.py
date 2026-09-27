#!/usr/bin/env python3
"""Optional manual E2E walkthrough (thin wrapper around ``lib.manual_e2e``)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.manual_e2e import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
