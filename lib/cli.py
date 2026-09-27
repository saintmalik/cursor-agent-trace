"""Cursor Agent TRACE CLI: ``python -m lib <command>``."""

from __future__ import annotations

import argparse
import sys

from . import demo, doctor, install_hooks, manual_e2e, verify_trace, view


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lib",
        description="Cursor Agent TRACE: install, doctor, verify, view, demo.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    install_hooks.add_parser_clean(sub)
    doctor.add_parser_clean(sub)
    verify_trace.add_parser_clean(sub)
    view.add_parser_clean(sub)
    demo.add_parser_clean(sub)
    manual_e2e.add_parser_clean(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
