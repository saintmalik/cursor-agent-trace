#!/usr/bin/env python3
"""Offline: JSONL activity trail → signed TRACE Trust Record."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import assemble  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trail", type=Path, help="Path to *.jsonl activity trail")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: <trail>.trace.json)",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="SPIFFE or DID subject (default: env or spiffe://local.cursor/agent/hooks)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="dump",
        help="Also print signed record JSON to stdout",
    )
    args = parser.parse_args()

    if not args.trail.is_file():
        print(f"error: trail not found: {args.trail}", file=sys.stderr)
        return 2

    try:
        dest, signed, identity = assemble.assemble_and_sign_from_trail(
            args.trail,
            out=args.out,
            subject=args.subject,
        )
    except assemble.MissingEvidence as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {dest}")
    print(f"model.provider={identity.provider} model_id={identity.model_id!r}")
    for note in identity.notes:
        print(f"note: {note}")
    if args.dump:
        print(json.dumps(signed, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
