"""Hermetic demo walkthrough: install → deny .env → trail → verify.

Also: ``python -m lib demo --policy-deny`` opens the viewer on a checked-in
fixture with mixed allow/deny timeline events (and an intentionally unsigned
TRACE so verify FAIL is visibly different from policy DENY).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import install_hooks, verify_trace

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "cursor-agent-trace-hook.py"
POLICY_DENY_FIXTURE = ROOT / "examples" / "policy-deny-demo.jsonl"


def _clear_cursor_env() -> None:
    for key in list(os.environ):
        if key.startswith("CURSOR_"):
            del os.environ[key]
    os.environ.pop("TRACE_PRIVATE_KEY_PEM", None)


def _hook_env(project: Path, demo_home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CURSOR_")
    }
    env["HOME"] = str(demo_home)
    env["CURSOR_PROJECT_DIR"] = str(project)
    return env


def _run_hook(project: Path, demo_home: Path, payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=_hook_env(project, demo_home),
        check=False,
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def run_demo(*, fast: bool = False) -> int:
    _clear_cursor_env()

    tmp = ROOT / ".tmp-demo"
    if tmp.exists():
        shutil.rmtree(tmp)
    project = tmp / "demo-project"
    demo_home = tmp / "home"
    project.mkdir(parents=True)
    demo_home.mkdir(parents=True)

    def pause() -> None:
        if not fast:
            time.sleep(0.35)

    def step(msg: str) -> None:
        print()
        print("══════════════════════════════════════════════════════════")
        print(f"▶ {msg}")
        print("══════════════════════════════════════════════════════════")
        pause()

    conv = f"demo-{int(time.time())}"

    print(
        "Cursor Agent TRACE — demo walkthrough\n"
        "=====================================\n"
        "This script simulates Cursor hooks locally (no live IDE required).\n"
        "Record with:  asciinema rec docs/demo.cast\n"
        "              terminalizer record docs/demo\n"
        "Then export a GIF to docs/demo.gif (or upload to YouTube) and link it\n"
        "from the README Demo section.\n"
        f"\nWorking tree: {project}"
    )
    pause()

    step("1. Install hooks into a demo project (writes env + policy)")
    rc = install_hooks.install(mode="project", target=project)
    if rc != 0:
        return rc
    print()
    print("Wrote:")
    for p in sorted((project / ".cursor").iterdir()):
        print(f"  {p.name}")
    print()
    print("Policy (enforce + deny .env):")
    pol = json.loads(
        (project / ".cursor" / "agent-trace.policy.json").read_text(encoding="utf-8")
    )
    print(json.dumps(pol, indent=2))
    pause()

    step("2. Agent tries to read .env → policy deny")
    out = _run_hook(
        project,
        demo_home,
        {
            "conversation_id": conv,
            "generation_id": "demo-gen",
            "model": "default",
            "model_id": "composer-2",
            "hook_event_name": "beforeReadFile",
            "file_path": str(project / ".env"),
            "tool_name": "Read",
            "cursor_version": "demo",
            "workspace_roots": [str(project)],
        },
    )
    print(f"hook stdout: {json.dumps(out)}")
    assert out.get("permission") == "deny", out
    print("✓ denied .env read")
    pause()

    step("3. Normal prompt + stop → JSONL trail + signed TRACE")
    _run_hook(
        project,
        demo_home,
        {
            "conversation_id": conv,
            "generation_id": "demo-gen",
            "model": "default",
            "model_id": "composer-2",
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "hello — please do not leak TOKEN=secret",
            "attachments": [],
        },
    )
    _run_hook(
        project,
        demo_home,
        {
            "conversation_id": conv,
            "generation_id": "demo-gen",
            "model": "default",
            "model_id": "composer-2",
            "hook_event_name": "stop",
            "status": "completed",
            "loop_count": 0,
        },
    )

    trail_dir = project / ".cursor" / "agent-trace"
    trail = trail_dir / f"{conv}.jsonl"
    trace = trail_dir / f"{conv}.trace.json"
    honesty = trail_dir / f"{conv}.honesty.json"
    assert trail.is_file() and trace.is_file() and honesty.is_file()
    print("Artifacts:")
    for p in sorted(trail_dir.glob(f"{conv}.*")):
        print(f"  {p.name}  ({p.stat().st_size} bytes)")
    pause()

    step("4. Peek at the activity trail (redacted)")
    rows = [json.loads(l) for l in trail.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        name = r.get("hook_event_name") or (r.get("payload") or {}).get("hook_event_name")
        dec = r.get("policy_decision")
        line = f"  • {name}"
        if dec:
            line += f"  policy={dec.get('decision')} ({dec.get('rule_kind') or ''})"
        print(line)
    blob = trail.read_text(encoding="utf-8")
    assert "TOKEN=secret" not in blob
    print("✓ secrets redacted from trail")
    pause()

    step("5. Verify the signed TRACE")
    rc = verify_trace.verify(trace)
    if rc != 0:
        return rc
    pause()

    step("Done")
    print(
        f"Trail:    {trail}\n"
        f"TRACE:    {trace}\n"
        f"Honesty:  {honesty}  (local sidecar — not part of the signed record)\n"
        "\n"
        "To record this demo later:\n"
        "  DEMO_FAST=1 python -m lib demo\n"
        "  # or: python examples/demo_script.py --fast\n"
        "  # asciinema: DEMO_FAST=1 asciinema rec -c 'python -m lib demo' docs/demo.cast"
    )
    return 0


def run_policy_deny_demo(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Open the viewer on the checked-in policy-deny fixture."""
    from . import view

    fixture = POLICY_DENY_FIXTURE
    if not fixture.is_file():
        print(f"error: missing fixture {fixture}", file=sys.stderr)
        return 2
    print(
        "Policy-deny demo fixture\n"
        f"  trail:   {fixture}\n"
        "  look for: red DENY / WOULD DENY in the timeline\n"
        "  filter:   Timeline → “Policy denials only”\n"
        "  verify:   Signed-record check shows FAIL (unsigned demo TRACE)\n"
        "  remember: policy DENY ≠ verify FAIL\n"
    )
    return view.serve(
        fixture,
        host=host,
        port=port,
        open_browser=open_browser,
        library=False,
        include_s3=False,
        extra_dirs=[fixture.parent],
    )


def add_parser_clean(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "demo",
        help="Recordable install → deny → trail walkthrough (or --policy-deny viewer)",
        description=__doc__,
    )
    p.add_argument(
        "--fast",
        action="store_true",
        help="Skip pauses (or set DEMO_FAST=1)",
    )
    p.add_argument(
        "--policy-deny",
        action="store_true",
        help="Open the viewer on examples/policy-deny-demo.jsonl (allow + deny + verify FAIL)",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host for --policy-deny")
    p.add_argument("--port", type=int, default=8765, help="Bind port for --policy-deny")
    p.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open a browser (--policy-deny)",
    )

    def _handler(args: argparse.Namespace) -> int:
        if args.policy_deny:
            return run_policy_deny_demo(
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
        return run_demo(fast=args.fast or os.environ.get("DEMO_FAST") == "1")

    p.set_defaults(_handler=_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_script.py", description=__doc__)
    parser.add_argument("--fast", action="store_true", help="Skip pauses")
    parser.add_argument(
        "--policy-deny",
        action="store_true",
        help="Open the viewer on examples/policy-deny-demo.jsonl",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    if args.policy_deny:
        return run_policy_deny_demo(
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
    fast = args.fast or os.environ.get("DEMO_FAST") == "1"
    return run_demo(fast=fast)


if __name__ == "__main__":
    raise SystemExit(main())
