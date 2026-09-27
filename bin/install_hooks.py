#!/usr/bin/env python3
"""Install Cursor Agent TRACE hooks (thin wrapper around ``lib.install_hooks``)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.install_hooks import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
