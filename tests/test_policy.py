"""Unit tests for lib.policy allow/deny evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import policy as pol


@pytest.fixture
def enforce(enforce_policy: Path) -> pol.Policy:
    return pol.load_policy()


@pytest.fixture
def observe(observe_policy: Path) -> pol.Policy:
    return pol.load_policy()


def test_deny_env_file_read(enforce: pol.Policy, project_dir: Path):
    decision = pol.evaluate(
        {
            "hook_event_name": "beforeReadFile",
            "file_path": str(project_dir / ".env"),
            "tool_name": "Read",
        },
        enforce,
    )
    assert decision.decision == "deny"
    assert decision.blocked is True
    assert decision.rule_kind == "file_read"
    assert decision.matched_rule in enforce.deny_file_read


def test_allow_normal_readme_read(enforce: pol.Policy, project_dir: Path):
    decision = pol.evaluate(
        {
            "hook_event_name": "beforeReadFile",
            "file_path": str(project_dir / "README.md"),
            "tool_name": "Read",
        },
        enforce,
    )
    assert decision.decision == "allow"
    assert decision.blocked is False


def test_deny_dangerous_shell(enforce: pol.Policy):
    decision = pol.evaluate(
        {
            "hook_event_name": "beforeShellExecution",
            "command": "sudo rm -rf /",
        },
        enforce,
    )
    assert decision.decision == "deny"
    assert decision.rule_kind == "shell"
    assert "rm -rf /" in decision.matched_rule


def test_allow_benign_shell(enforce: pol.Policy):
    decision = pol.evaluate(
        {
            "hook_event_name": "beforeShellExecution",
            "command": "echo hello",
        },
        enforce,
    )
    assert decision.decision == "allow"


def test_observe_mode_would_deny_but_not_block(observe: pol.Policy, project_dir: Path):
    decision = pol.evaluate(
        {
            "hook_event_name": "beforeReadFile",
            "file_path": str(project_dir / ".env"),
        },
        observe,
    )
    assert decision.decision == "would_deny"
    assert decision.blocked is False
    assert decision.mode == "observe"
    assert observe.trace_enforcement_mode == "advisory"


def test_enforce_maps_to_trace_enforcement_mode(enforce: pol.Policy):
    assert enforce.trace_enforcement_mode == "enforce"


def test_path_matches_glob_variants():
    assert pol.path_matches("/home/u/project/.env", ".env")
    assert pol.path_matches("/home/u/project/.env.local", ".env.*")
    assert pol.path_matches("/home/u/secrets/credentials.json", "**/credentials.json")
    assert pol.path_matches("/tmp/my-secret-key", "**/*secret*")
    assert not pol.path_matches("/home/u/README.md", ".env")


def test_load_policy_prefers_project_over_home(
    project_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home_pol = Path.home() / ".cursor" / "agent-trace.policy.json"
    home_pol.parent.mkdir(parents=True, exist_ok=True)
    home_pol.write_text(
        json.dumps({"mode": "observe", "deny_file_read": [], "deny_shell_patterns": []})
        + "\n",
        encoding="utf-8",
    )
    proj = project_dir / ".cursor" / "agent-trace.policy.json"
    proj.write_text(
        json.dumps(
            {
                "mode": "enforce",
                "deny_file_read": [".env"],
                "deny_shell_patterns": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = pol.load_policy()
    assert loaded.mode == "enforce"
    assert loaded.deny_file_read == [".env"]
    assert str(proj) in loaded.source


def test_non_gating_event_always_allows(enforce: pol.Policy):
    decision = pol.evaluate(
        {"hook_event_name": "afterAgentResponse", "text": "hi"},
        enforce,
    )
    assert decision.decision == "allow"
    assert "non-gating" in decision.reason


def test_decision_to_record_shape(enforce: pol.Policy, project_dir: Path):
    decision = pol.evaluate(
        {
            "hook_event_name": "beforeReadFile",
            "file_path": str(project_dir / ".env"),
        },
        enforce,
    )
    record = pol.decision_to_record(decision, enforce)
    assert record["decision"] == "deny"
    assert record["trace_enforcement_mode"] == "enforce"
    assert record["policy_source"]
