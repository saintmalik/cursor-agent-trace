"""Hermetic pytest fixtures — no real network, no user's ~/.cursor."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "bin" / "cursor-agent-trace-hook.py"

# Captured before hermetic HOME override — needed for subprocesses that must
# still see user-site packages (e.g. click for the trace-tests CLI).
REAL_HOME = os.environ.get("HOME") or str(Path.home())


@pytest.fixture
def real_home() -> str:
    return REAL_HOME


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate every test from the operator's HOME / Cursor project / CURSOR_* knobs."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)

    # Drop feature flags that would leak from the developer shell.
    for key in list(os.environ):
        if key.startswith("CURSOR_") or key.startswith("COLLECTOR_") or key in {
            "TRACE_PRIVATE_KEY_PEM",
            "S3_BUCKET",
            "AWS_S3_BUCKET",
            "PREFIX",
            "LOCAL_DIR",
        }:
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(project))
    # Keep Path.home() consistent with HOME on platforms that cache it.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    yield


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "project"


@pytest.fixture
def trail_dir(project_dir: Path) -> Path:
    d = project_dir / ".cursor" / "agent-trace"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def enforce_policy(project_dir: Path) -> Path:
    """Copy shipping enforce policy into the fake project."""
    src = ROOT / "templates" / "agent-trace.policy.json"
    dest = project_dir / ".cursor" / "agent-trace.policy.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    return dest


@pytest.fixture
def observe_policy(project_dir: Path) -> Path:
    src = ROOT / "templates" / "agent-trace.policy.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    data["mode"] = "observe"
    dest = project_dir / ".cursor" / "agent-trace.policy.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return dest


@pytest.fixture
def sample_events() -> list[dict[str, Any]]:
    """Minimal hook trail with a concrete model_id (not generic default)."""
    return [
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "conv-test-1",
            "generation_id": "gen-1",
            "model": "default",
            "model_id": "composer-2",
            "payload": {"prompt": "hello"},
        },
        {
            "hook_event_name": "preToolUse",
            "conversation_id": "conv-test-1",
            "tool_name": "Shell",
            "tool_use_id": "t1",
            "tool_input": {"command": "echo hi"},
        },
        {
            "hook_event_name": "postToolUse",
            "conversation_id": "conv-test-1",
            "tool_name": "Shell",
            "tool_use_id": "t1",
            "tool_output": "hi",
        },
        {
            "hook_event_name": "stop",
            "conversation_id": "conv-test-1",
            "status": "completed",
        },
    ]


@pytest.fixture
def write_trail(trail_dir: Path, sample_events: list[dict[str, Any]]):
    def _write(name: str = "conv-test-1.jsonl", events: list[dict[str, Any]] | None = None) -> Path:
        path = trail_dir / name
        rows = events if events is not None else sample_events
        path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in rows),
            encoding="utf-8",
        )
        return path

    return _write


def _load_hook_module():
    """Import the hook script as a module (after hermetic env is set)."""
    # Avoid colliding with a previously loaded hook under a different HOME.
    name = "cursor_agent_trace_hook_under_test"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    # Reset config-loaded flag if reloading.
    spec.loader.exec_module(mod)
    if hasattr(mod, "_CONFIG_LOADED"):
        mod._CONFIG_LOADED = False
    return mod


@pytest.fixture
def hook_mod(monkeypatch: pytest.MonkeyPatch):
    """Fresh hook module with config reload available."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "_CONFIG_LOADED", False)
    return mod


def pytest_configure(config: pytest.Config) -> None:
    # Ensure repo root is importable as ``lib``.
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
