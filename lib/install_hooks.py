"""Install Cursor Agent TRACE hooks into a project or user Cursor config."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK_PY = ROOT / "bin" / "cursor-agent-trace-hook.py"
POLICY_TEMPLATE = ROOT / "templates" / "agent-trace.policy.json"
HOOKS_TEMPLATE = ROOT / "templates" / "hooks.template.json"


def _merge_hooks(
    hooks_json: Path,
    cmd: str,
    *,
    dry_run: bool,
    remove_legacy: bool,
    quiet: bool = False,
) -> None:
    template = json.loads(HOOKS_TEMPLATE.read_text(encoding="utf-8"))
    wanted = template["hooks"]

    if hooks_json.exists():
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    else:
        data = {"version": 1, "hooks": {}}
    data["version"] = 1
    hooks = data.setdefault("hooks", {})

    removed = 0
    if remove_legacy:
        for event, entries in list(hooks.items()):
            kept = []
            for e in entries:
                if isinstance(e, dict):
                    c = str(e.get("command") or "")
                    if "cursor-activity-hook.py" in c:
                        removed += 1
                        continue
                kept.append(e)
            hooks[event] = kept

    added = skipped = 0
    for event, entries in wanted.items():
        existing = hooks.setdefault(event, [])
        already = any(isinstance(e, dict) and e.get("command") == cmd for e in existing)
        if already:
            skipped += 1
            continue
        entry = dict(entries[0])
        entry["command"] = cmd
        existing.append(entry)
        added += 1

    if not quiet:
        print(f"target={hooks_json}")
        print(f"command={cmd}")
        print(f"events_added={added} events_already_present={skipped} legacy_removed={removed}")
    if dry_run:
        if not quiet:
            print(json.dumps(data, indent=2))
    else:
        hooks_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if not quiet:
            print("wrote hooks.json")


def _write_env(
    path: Path,
    *,
    redact: str,
    s3_bucket: str,
    collector_url: str,
    collector_token: str,
    quiet: bool = False,
) -> None:
    s3_on = "1" if s3_bucket else "0"
    desired: dict[str, str] = {
        "CURSOR_AGENT_TRACE_REDACT": redact,
        "CURSOR_AGENT_TRACE_SIGN": "1",
        "CURSOR_AGENT_TRACE_S3": s3_on,
        "COLLECTOR_BATCH_SIZE": "10",
        "COLLECTOR_FLUSH_INTERVAL_SEC": "2",
        "COLLECTOR_RETRY_BASE_SEC": "1",
        "COLLECTOR_RETRY_MAX_SEC": "60",
        "COLLECTOR_RETRY_MAX_ATTEMPTS": "0",
        "COLLECTOR_TIMEOUT": "5",
    }
    if s3_bucket:
        desired["CURSOR_AGENT_TRACE_S3_BUCKET"] = s3_bucket
        desired["CURSOR_AGENT_TRACE_S3"] = "1"
    if collector_url:
        desired["COLLECTOR_URL"] = collector_url.rstrip("/")
    if collector_token:
        desired["COLLECTOR_TOKEN"] = collector_token

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    found: set[str] = set()

    for line in lines:
        stripped = line.strip()
        key_line = stripped[7:].strip() if stripped.startswith("export ") else stripped
        if "=" in key_line and not key_line.startswith("#"):
            key = key_line.split("=", 1)[0].strip()
            if key in desired:
                out.append(f"{key}={desired[key]}")
                found.add(key)
                continue
        out.append(line)

    missing = [k for k in desired if k not in found]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# Written by install-hooks (process env overrides; no shell exports needed)")
        for key in desired:
            if key not in found:
                out.append(f"{key}={desired[key]}")
                found.add(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    if quiet:
        return
    print(f"wrote {path}")
    for key, value in desired.items():
        if key == "COLLECTOR_TOKEN" and value:
            shown = value[:4] + "…" if len(value) > 4 else "****"
            print(f"  {key}={shown}")
        else:
            print(f"  {key}={value}")


def install(
    *,
    mode: str = "project",
    target: str | Path | None = None,
    redact: str = "safe",
    s3_bucket: str = "",
    collector_url: str = "",
    collector_token: str = "",
    remove_legacy: bool = True,
    dry_run: bool = False,
    quiet: bool = False,
) -> int:
    if redact not in {"safe", "full", "off", "none", "raw"}:
        print(f"Invalid --redact value: {redact} (use safe|full)", file=sys.stderr)
        return 2
    if s3_bucket.startswith("-"):
        print(f"Invalid --s3-bucket value: {s3_bucket}", file=sys.stderr)
        return 2
    if collector_url.startswith("-"):
        print(f"Invalid --collector-url value: {collector_url}", file=sys.stderr)
        return 2
    if collector_url and not collector_token and not quiet:
        print(
            "Warning: --collector-url set without --collector-token "
            "(hook will log auth errors)",
            file=sys.stderr,
        )
    if not collector_url and collector_token and not quiet:
        print(
            "Warning: --collector-token set without --collector-url "
            "(collector stays off)",
            file=sys.stderr,
        )

    for path in (HOOK_PY, ROOT / "bin" / "jsonl-to-trace.py"):
        try:
            path.chmod(path.stat().st_mode | 0o111)
        except OSError:
            pass

    if mode == "user":
        cursor_dir = Path.home() / ".cursor"
    else:
        base = Path(target) if target else Path.cwd()
        base = base.resolve()
        cursor_dir = base / ".cursor"

    hooks_json = cursor_dir / "hooks.json"
    env_file = cursor_dir / "agent-trace.env"
    policy_file = cursor_dir / "agent-trace.policy.json"
    cursor_dir.mkdir(parents=True, exist_ok=True)

    if not POLICY_TEMPLATE.is_file():
        print(f"Missing policy template: {POLICY_TEMPLATE}", file=sys.stderr)
        return 1

    _merge_hooks(
        hooks_json,
        str(HOOK_PY),
        dry_run=dry_run,
        remove_legacy=remove_legacy,
        quiet=quiet,
    )

    s3_on = 1 if s3_bucket else 0
    if dry_run:
        if not quiet:
            print(
                f"would write {env_file} (redact={redact} sign=1 s3={s3_on} "
                f"bucket={s3_bucket or ''} collector={collector_url or ''})"
            )
            if policy_file.is_file():
                print(f"would keep existing {policy_file} (not overwritten)")
            else:
                print(f"would write {policy_file} from template (mode=enforce)")
    else:
        if policy_file.is_file():
            if not quiet:
                print(f"kept existing {policy_file}")
        else:
            policy_file.write_bytes(POLICY_TEMPLATE.read_bytes())
            if not quiet:
                print(f"wrote {policy_file} (mode=enforce; edit to switch observe)")
        _write_env(
            env_file,
            redact=redact,
            s3_bucket=s3_bucket,
            collector_url=collector_url,
            collector_token=collector_token,
            quiet=quiet,
        )

    if quiet:
        if not dry_run:
            print(f"ok hooks={hooks_json} env={env_file} policy={policy_file}")
        return 0

    print()
    print("Install complete.")
    print(f"  Config: {env_file}")
    print(f"  Policy: {policy_file}")
    print(f"  Hooks:  {hooks_json}")
    if collector_url:
        print(
            f"  Collector: {collector_url} "
            "(batched POST + outbox replay; fail-open if down)"
        )
    print()
    print("Confirm the hook under Customize → Hooks, then send an Agent message.")
    print("Artifacts: <project>/.cursor/agent-trace/<id>.jsonl and <id>.trace.json")
    print()
    print("Check: python -m lib doctor")
    return 0


def _add_install_flags(parser: argparse.ArgumentParser) -> None:
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--user", action="store_true", help="Merge into ~/.cursor/hooks.json")
    g.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Install into <dir>/.cursor/hooks.json (default: cwd)",
    )
    parser.add_argument(
        "--redact",
        default="safe",
        metavar="MODE",
        help="Privacy redact in written config (default: safe)",
    )
    parser.add_argument(
        "--s3-bucket",
        default="",
        metavar="NAME",
        help="Enable direct S3 upload (prefer collector for teams)",
    )
    parser.add_argument(
        "--collector-url",
        default="",
        metavar="URL",
        help="Org collector base URL",
    )
    parser.add_argument(
        "--collector-token",
        default="",
        metavar="TOK",
        help="Bearer token for the org collector",
    )
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="Do not remove old cursor-activity-hook.py entries",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes only")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Minimal output (for fleet / unattended installs)",
    )
    parser.add_argument(
        "--noninteractive",
        action="store_true",
        help="Alias for --quiet (no prompts; deterministic paths)",
    )


def build_arg_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "install-hooks",
        description=__doc__,
    )
    _add_install_flags(parser)
    return parser


def run_from_args(args: argparse.Namespace) -> int:
    quiet = bool(getattr(args, "quiet", False) or getattr(args, "noninteractive", False))
    return install(
        mode="user" if args.user else "project",
        target=args.project,
        redact=args.redact,
        s3_bucket=args.s3_bucket or "",
        collector_url=args.collector_url or "",
        collector_token=args.collector_token or "",
        remove_legacy=not args.keep_legacy,
        dry_run=args.dry_run,
        quiet=quiet,
    )


def add_parser_clean(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "install-hooks",
        help="Install hooks into a project or ~/.cursor",
        description=__doc__,
    )
    _add_install_flags(p)
    p.set_defaults(_handler=run_from_args)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser(prog="install_hooks.py")
    return run_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
