"""Verify a signed TRACE record with agentrust-trace-tests (trace-tests CLI)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def verify(record: Path) -> int:
    record = Path(record)
    if not record.is_file():
        print(f"error: record not found: {record}", file=sys.stderr)
        return 2

    exe = shutil.which("trace-tests")
    if not exe:
        print(
            "error: trace-tests CLI not found. "
            "Install: pip install agentrust-trace-tests",
            file=sys.stderr,
        )
        return 2

    print("== verify signed TRACE ==")
    print(f"record: {record}")
    # Schema conformance: agentrust-trace-tests checks software-observed TRACE at --level 0.
    proc = subprocess.run(
        [exe, "verify", "--record", str(record), "--level", "0"],
        check=False,
    )
    return proc.returncode


def build_arg_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "verify",
        description=__doc__,
        epilog=(
            "Requires: pip install 'agentrust-trace>=0.9' 'agentrust-trace-tests>=0.5'"
        ),
    )
    parser.add_argument("record", type=Path, help="Path to *.trace.json")
    return parser


def run_from_args(args: argparse.Namespace) -> int:
    return verify(args.record)


def add_parser_clean(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "verify",
        help="Verify a signed TRACE with trace-tests",
        description=__doc__,
    )
    p.add_argument("record", type=Path, help="Path to *.trace.json")
    p.set_defaults(_handler=run_from_args)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser(prog="verify_trace.py")
    args = parser.parse_args(argv)
    return run_from_args(args)


if __name__ == "__main__":
    raise SystemExit(main())
