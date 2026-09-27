"""Allow/deny policy for Cursor Agent TRACE hooks.

Default mode is ``enforce`` with a narrow high-value deny list (secrets files,
obviously destructive shell). ``observe`` evaluates the same rules but always
allows — decisions are still written into the JSONL trail.

TRACE ``policy.enforcement_mode`` maps:
  enforce  → "enforce"
  observe  → "advisory"
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath, Path
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "enforce",
    "deny_file_read": [
        ".env",
        ".env.*",
        "**/.env",
        "**/credentials.json",
        "**/*secret*",
    ],
    "deny_shell_patterns": [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "dd if=",
    ],
    "deny_tools": [],
}

ENFORCEMENT_EVENTS = frozenset(
    {
        "preToolUse",
        "beforeShellExecution",
        "beforeMCPExecution",
        "beforeReadFile",
        "beforeTabFileRead",
    }
)


@dataclass
class Policy:
    mode: str = "enforce"  # enforce | observe
    deny_file_read: list[str] = field(default_factory=list)
    deny_shell_patterns: list[str] = field(default_factory=list)
    deny_tools: list[str] = field(default_factory=list)
    source: str = "baked-defaults"
    raw_bytes: bytes = b""

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    @property
    def trace_enforcement_mode(self) -> str:
        """Value for TRACE policy.enforcement_mode (schema enum)."""
        return "enforce" if self.enforcing else "advisory"


@dataclass
class Decision:
    decision: str  # allow | deny | would_deny
    reason: str = ""
    matched_rule: str = ""
    rule_kind: str = ""  # file_read | shell | tool
    mode: str = "enforce"

    @property
    def blocked(self) -> bool:
        return self.decision == "deny"


def default_policy_template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "templates" / "agent-trace.policy.json"


def policy_candidate_paths() -> list[Path]:
    """Resolve order: explicit env → project → user home."""
    candidates: list[Path] = []
    for env_key in ("CURSOR_AGENT_TRACE_POLICY_JSON", "CURSOR_AGENT_TRACE_POLICY"):
        explicit = os.environ.get(env_key)
        if not explicit:
            continue
        path = Path(explicit).expanduser()
        # Ignore legacy .txt policy-bundle paths — those are for TRACE hashing only.
        if path.suffix.lower() in {".json"} or path.name.endswith(".policy.json"):
            candidates.append(path)
    project = os.environ.get("CURSOR_PROJECT_DIR")
    if project:
        candidates.append(Path(project) / ".cursor" / "agent-trace.policy.json")
    candidates.append(Path.home() / ".cursor" / "agent-trace.policy.json")
    return candidates


def _normalize_mode(raw: Any) -> str:
    mode = str(raw or "enforce").strip().lower()
    if mode in {"observe", "advisory", "audit", "log"}:
        return "observe"
    return "enforce"


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def policy_from_dict(data: dict[str, Any], *, source: str, raw: bytes) -> Policy:
    return Policy(
        mode=_normalize_mode(data.get("mode")),
        deny_file_read=_as_str_list(data.get("deny_file_read")),
        deny_shell_patterns=_as_str_list(data.get("deny_shell_patterns")),
        deny_tools=_as_str_list(data.get("deny_tools")),
        source=source,
        raw_bytes=raw,
    )


def baked_policy() -> Policy:
    raw = json.dumps(DEFAULT_POLICY, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    return policy_from_dict(DEFAULT_POLICY, source="baked-defaults", raw=raw)


def load_policy(*, force_reload: bool = False) -> Policy:
    """Load first readable policy JSON; else baked defaults.

    ``force_reload`` is accepted for tests; this module does not cache.
    """
    del force_reload  # reserved; always fresh for now
    for path in policy_candidate_paths():
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        return policy_from_dict(data, source=str(path), raw=raw)
    # Prefer shipping template bytes when present (stable hash across installs).
    template = default_policy_template_path()
    if template.is_file():
        try:
            raw = template.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return policy_from_dict(data, source=f"template:{template}", raw=raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return baked_policy()


def path_matches(path: str, pattern: str) -> bool:
    """Glob-style match for deny_file_read patterns (supports ``**``)."""
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        return False
    p = PurePosixPath(normalized)
    name = PurePosixPath(p.name)
    if p.match(pattern) or name.match(pattern):
        return True
    if not pattern.startswith("**/"):
        if p.match("**/" + pattern) or name.match(pattern):
            return True
    return False


def _file_path_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("file_path", "path", "filePath", "uri"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("path", "file_path", "filePath", "target_file", "file"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _shell_command_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("command", "shell_command"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        value = tool_input.get("command")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_name_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("tool_name", "toolName", "name", "mcp_tool"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _finish(policy: Policy, *, matched: bool, kind: str, rule: str, detail: str) -> Decision:
    if not matched:
        return Decision(decision="allow", mode=policy.mode, reason="no deny rule matched")
    if policy.enforcing:
        return Decision(
            decision="deny",
            mode=policy.mode,
            reason=detail,
            matched_rule=rule,
            rule_kind=kind,
        )
    return Decision(
        decision="would_deny",
        mode=policy.mode,
        reason=detail,
        matched_rule=rule,
        rule_kind=kind,
    )


def evaluate(payload: dict[str, Any], policy: Policy | None = None) -> Decision:
    """Return allow/deny/would_deny for a hook payload."""
    pol = policy or load_policy()
    event = str(payload.get("hook_event_name") or "")
    if event not in ENFORCEMENT_EVENTS:
        return Decision(decision="allow", mode=pol.mode, reason="non-gating event")

    tool = _tool_name_from_payload(payload)
    if tool and pol.deny_tools:
        tool_l = tool.lower()
        for denied in pol.deny_tools:
            if tool_l == denied.lower():
                return _finish(
                    pol,
                    matched=True,
                    kind="tool",
                    rule=denied,
                    detail=f"tool {tool!r} is denied",
                )

    # File reads: beforeReadFile / beforeTabFileRead / Read-like preToolUse
    file_path = _file_path_from_payload(payload)
    check_file = event in {"beforeReadFile", "beforeTabFileRead"} or (
        event == "preToolUse" and tool and tool.lower() in {"read", "readfile", "tabread"}
    )
    if check_file and file_path and pol.deny_file_read:
        for pattern in pol.deny_file_read:
            if path_matches(file_path, pattern):
                return _finish(
                    pol,
                    matched=True,
                    kind="file_read",
                    rule=pattern,
                    detail=f"file read {file_path!r} matches deny pattern {pattern!r}",
                )

    # Shell: beforeShellExecution / Shell preToolUse
    command = _shell_command_from_payload(payload)
    check_shell = event == "beforeShellExecution" or (
        event == "preToolUse" and tool and tool.lower() in {"shell", "bash", "zsh"}
    )
    if check_shell and command and pol.deny_shell_patterns:
        for pattern in pol.deny_shell_patterns:
            if pattern in command:
                return _finish(
                    pol,
                    matched=True,
                    kind="shell",
                    rule=pattern,
                    detail=f"shell command matches deny pattern {pattern!r}",
                )

    return Decision(decision="allow", mode=pol.mode, reason="no deny rule matched")


def decision_to_record(decision: Decision, policy: Policy) -> dict[str, Any]:
    return {
        "mode": decision.mode,
        "decision": decision.decision,
        "reason": decision.reason,
        "matched_rule": decision.matched_rule or None,
        "rule_kind": decision.rule_kind or None,
        "policy_source": policy.source,
        "trace_enforcement_mode": policy.trace_enforcement_mode,
    }
