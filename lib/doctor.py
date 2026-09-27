"""Diagnose Cursor Agent TRACE install readiness (``python -m lib doctor``)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HOOK_PY = ROOT / "bin" / "cursor-agent-trace-hook.py"
HOOK_NEEDLE = "cursor-agent-trace-hook.py"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hard: bool = True


def _line(check: Check) -> str:
    tag = "OK  " if check.ok else ("FAIL" if check.hard else "WARN")
    return f"{tag}  {check.name}: {check.detail}"


def _hooks_point_at(hooks_json: Path, needle: str) -> bool:
    if not hooks_json.is_file():
        return False
    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and needle in str(entry.get("command") or ""):
                return True
    return False


def _policy_info(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"missing ({path})"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"unreadable ({path}): {exc}"
    mode = str(data.get("mode") or "unknown")
    return True, f"mode={mode} path={path}"


def _latest_artifacts(trail_roots: list[Path]) -> tuple[Path | None, Path | None]:
    latest_jsonl: Path | None = None
    latest_trace: Path | None = None
    latest_jsonl_mtime = -1.0
    latest_trace_mtime = -1.0
    for root in trail_roots:
        if not root.is_dir():
            continue
        for path in root.glob("*.jsonl"):
            if path.name.endswith(".collector.jsonl") or path.name.endswith(".s3.jsonl"):
                continue
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if m > latest_jsonl_mtime:
                latest_jsonl_mtime = m
                latest_jsonl = path
        for path in root.glob("*.trace.json"):
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if m > latest_trace_mtime:
                latest_trace_mtime = m
                latest_trace = path
    return latest_jsonl, latest_trace


def _env_file_keys(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def run_checks(*, project: Path | None = None) -> list[Check]:
    home = Path.home()
    project = (project or Path(os.environ.get("CURSOR_PROJECT_DIR") or Path.cwd())).resolve()
    checks: list[Check] = []

    if HOOK_PY.is_file():
        checks.append(Check("hook-script", True, str(HOOK_PY)))
    else:
        checks.append(Check("hook-script", False, f"missing {HOOK_PY}"))

    user_hooks = home / ".cursor" / "hooks.json"
    project_hooks = project / ".cursor" / "hooks.json"
    user_ok = _hooks_point_at(user_hooks, HOOK_NEEDLE)
    project_ok = _hooks_point_at(project_hooks, HOOK_NEEDLE)
    if user_ok or project_ok:
        parts = []
        if user_ok:
            parts.append(f"user={user_hooks}")
        if project_ok:
            parts.append(f"project={project_hooks}")
        checks.append(Check("hooks", True, "; ".join(parts)))
    else:
        checks.append(
            Check(
                "hooks",
                False,
                f"not found (checked {user_hooks} and {project_hooks}). "
                "Run: python -m lib install-hooks --user",
            )
        )

    policy_candidates = [
        project / ".cursor" / "agent-trace.policy.json",
        home / ".cursor" / "agent-trace.policy.json",
    ]
    policy_ok = False
    policy_detail = ""
    for cand in policy_candidates:
        ok, detail = _policy_info(cand)
        if ok:
            policy_ok = True
            policy_detail = detail
            break
        if not policy_detail:
            policy_detail = detail
    if policy_ok:
        checks.append(Check("policy", True, policy_detail))
    else:
        template = ROOT / "templates" / "agent-trace.policy.json"
        if template.is_file():
            checks.append(
                Check(
                    "policy",
                    True,
                    f"no user/project policy yet; template at {template}",
                    hard=False,
                )
            )
        else:
            checks.append(Check("policy", False, policy_detail or "missing"))

    trail_roots = [
        project / ".cursor" / "agent-trace",
        home / ".cursor" / "agent-trace",
    ]
    explicit = os.environ.get("CURSOR_AGENT_TRACE_DIR")
    if explicit:
        trail_roots.insert(0, Path(explicit).expanduser())
    latest_jsonl, latest_trace = _latest_artifacts(trail_roots)
    if latest_jsonl:
        checks.append(Check("last-trail", True, str(latest_jsonl), hard=False))
    else:
        checks.append(
            Check(
                "last-trail",
                True,
                "none yet (send an Agent message after hooks are installed)",
                hard=False,
            )
        )
    if latest_trace:
        checks.append(Check("last-trace", True, str(latest_trace), hard=False))
    else:
        checks.append(
            Check("last-trace", True, "none yet (signed on stop/sessionEnd)", hard=False)
        )

    env_paths = [
        project / ".cursor" / "agent-trace.env",
        home / ".cursor" / "agent-trace.env",
    ]
    merged: dict[str, str] = {}
    for ep in reversed(env_paths):
        merged.update(_env_file_keys(ep))
    for key in (
        "CURSOR_AGENT_TRACE_SIGN",
        "TRACE_PRIVATE_KEY_PEM",
        "COLLECTOR_URL",
        "COLLECTOR_TOKEN",
    ):
        if key in os.environ:
            merged[key] = os.environ[key]

    sign = (merged.get("CURSOR_AGENT_TRACE_SIGN") or "1").strip()
    if sign in {"0", "false", "off", "no"}:
        checks.append(Check("signing", True, "disabled (CURSOR_AGENT_TRACE_SIGN=0)", hard=False))
    elif merged.get("TRACE_PRIVATE_KEY_PEM"):
        checks.append(Check("signing", True, "TRACE_PRIVATE_KEY_PEM set (pinned key)"))
    else:
        checks.append(
            Check(
                "signing",
                True,
                "enabled; ephemeral Ed25519 when TRACE_PRIVATE_KEY_PEM unset",
                hard=False,
            )
        )

    collector = (merged.get("COLLECTOR_URL") or "").strip()
    if collector:
        token = (merged.get("COLLECTOR_TOKEN") or "").strip()
        if token:
            checks.append(Check("collector", True, f"url={collector} token=set", hard=False))
        else:
            checks.append(
                Check(
                    "collector",
                    False,
                    f"COLLECTOR_URL set ({collector}) but COLLECTOR_TOKEN missing",
                    hard=False,
                )
            )
    else:
        checks.append(Check("collector", True, "optional; not configured", hard=False))

    if importlib.util.find_spec("agentrust_trace") is not None:
        checks.append(Check("agentrust-trace", True, "importable"))
    else:
        checks.append(
            Check(
                "agentrust-trace",
                False,
                "not importable (pip install -r requirements.txt)",
            )
        )

    return checks


def doctor(*, project: Path | None = None, json_out: bool = False) -> int:
    checks = run_checks(project=project)
    hard_fails = [c for c in checks if not c.ok and c.hard]

    if json_out:
        payload: dict[str, Any] = {
            "ok": not hard_fails,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "hard": c.hard,
                    "detail": c.detail,
                }
                for c in checks
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print("Cursor Agent TRACE doctor")
        for c in checks:
            print(_line(c))
        if hard_fails:
            print(f"\n{len(hard_fails)} hard failure(s).")
        else:
            print("\nReady (no hard failures).")

    return 1 if hard_fails else 0


def build_arg_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "doctor",
        description="Check hooks, policy, trails, signing, and deps.",
    )
    parser.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Project directory to inspect (default: cwd / CURSOR_PROJECT_DIR)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def run_from_args(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve() if args.project else None
    return doctor(project=project, json_out=bool(args.json))


def add_parser_clean(subparsers: argparse._SubParsersAction) -> None:
    for name, help_text in (
        ("doctor", "Check hooks, policy, trails, signing, and deps"),
        ("status", "Alias for doctor"),
    ):
        p = subparsers.add_parser(name, help=help_text, description=__doc__)
        p.add_argument("--project", metavar="DIR", default=None)
        p.add_argument("--json", action="store_true")
        p.set_defaults(_handler=run_from_args)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser(prog="doctor.py")
    return run_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
