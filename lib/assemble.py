"""Assemble and sign a software-observed TRACE Trust Record from a Cursor activity JSONL trail.

Claims only what agentrust-trace-tests accepts for software-observed TRACE (--level 0):
  - runtime.platform: software-only
  - no TEE / witness / weights attestation
  - policy.enforcement_mode: "enforce" when agent-trace.policy.json mode is
    enforce (hooks deny); "advisory" when mode is observe; "declared" only as
    a legacy fallback when no JSON policy is resolvable
  - appraisal.status: none
  - origin: third-party-control-plane (Cursor hook payloads are vendor-asserted)
  - model.provider: cursor-asserted from hooks; model_id may honestly be
    \"default\" / \"unknown\" (never invent a product slug). Lab override only:
    operator-declared via CURSOR_AGENT_TRACE_MODEL_ID.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .model_identity import ModelIdentity, resolve_model_identity
from . import policy as _policy

VERSION = "0.2.0"
TRACE_PROFILE = "tag:agentrust-io.com,2026:trace-v0.2"
PRODUCER = f"cursor-agent-trace@{VERSION}"
DEFAULT_SUBJECT = "spiffe://local.cursor/agent/hooks"
DEFAULT_DATA_CLASS = "internal"
DEFAULT_APPRAISAL_VERIFIER = "https://cursor.com/docs/hooks"
_SUBJECT_RE = re.compile(r"^(spiffe://[^/]+/.+|did:[a-z0-9]+:.+)$")
_DIGEST_RE = re.compile(r"^sha(256:[0-9a-f]{64}|384:[0-9a-f]{96})$")

# Hook events that imply a tool (or tool-like) invocation started / finished.
_TOOL_START = frozenset(
    {
        "preToolUse",
        "beforeShellExecution",
        "beforeMCPExecution",
        "beforeReadFile",
        "beforeTabFileRead",
        "subagentStart",
    }
)
_TOOL_OK = frozenset(
    {
        "postToolUse",
        "afterShellExecution",
        "afterMCPExecution",
        "afterFileEdit",
        "afterTabFileEdit",
        "subagentStop",
    }
)
_TOOL_ERR = frozenset({"postToolUseFailure"})


class MissingEvidence(ValueError):
    """Raised rather than inventing a required TRACE field."""


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def load_jsonl_events(path: Path) -> list[dict[str, Any]]:
    """Load activity JSONL lines (one hook event record per line)."""
    events: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MissingEvidence(f"{path}:{line_no}: invalid JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise MissingEvidence(f"{path}:{line_no}: expected object, got {type(row).__name__}")
        events.append(row)
    return events


def _tool_name(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for source in (event, payload):
        if not isinstance(source, dict):
            continue
        for key in ("tool_name", "toolName", "name", "command", "mcp_tool", "server"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        tool_input = source.get("tool_input")
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command")
            if isinstance(cmd, str) and cmd.strip():
                return "Shell"
    hook = event.get("hook_event_name") or (payload.get("hook_event_name") if isinstance(payload, dict) else None)
    return str(hook or "<unnamed>")


def _tool_id(event: dict[str, Any], index: int) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for source in (event, payload):
        if not isinstance(source, dict):
            continue
        for key in ("tool_use_id", "toolUseId", "call_id", "id", "generation_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"event-{index}"


def build_tool_transcript(events: list[dict[str, Any]]) -> tuple[bytes, int]:
    """Canonical tool identity transcript — names/ids/outcomes only, no payloads.

    A Trust Record may be handed to a third party, so arguments and outputs must
    not enter the hash.
    """
    calls: list[dict[str, Any]] = []
    pending: dict[str, str] = {}

    for index, event in enumerate(events):
        name = event.get("hook_event_name") or ""
        if not isinstance(name, str):
            continue
        tool_id = _tool_id(event, index)
        if name in _TOOL_START:
            pending[tool_id] = _tool_name(event)
            continue
        if name in _TOOL_OK or name in _TOOL_ERR:
            outcome = "error" if name in _TOOL_ERR else "ok"
            tool = pending.pop(tool_id, None) or _tool_name(event)
            if tool == name:
                tool = "<unobserved-start>"
            calls.append(
                {
                    "tool": tool,
                    "run_id": tool_id,
                    "parent_run_id": None,
                    "outcome": outcome,
                }
            )

    # Starts with no matching end: still record as unobserved-end.
    for tool_id, tool in pending.items():
        calls.append(
            {
                "tool": tool,
                "run_id": tool_id,
                "parent_run_id": None,
                "outcome": "unobserved-end",
            }
        )

    blob = json.dumps(calls, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return blob, len(calls)


def default_policy_bundle_path() -> Path:
    """Legacy observational bundle (used only when no JSON policy is available)."""
    return Path(__file__).resolve().parent.parent / "policy" / "declared-observe.txt"


def resolve_policy_for_trace() -> tuple[bytes, str]:
    """Return (policy_bundle_bytes, enforcement_mode) for the TRACE record.

    Prefer the runtime JSON allow/deny policy (hashed as evaluated). Fall back
    to declared-observe.txt + ``declared`` when nothing else is available.
    """
    pol = _policy.load_policy()
    if pol.raw_bytes:
        return pol.raw_bytes, pol.trace_enforcement_mode
    path = default_policy_bundle_path()
    if path.is_file():
        return path.read_bytes(), "declared"
    raise MissingEvidence("no policy bundle available for TRACE")


def workload_digest(*, hook_script: Path | None = None, assemble_module: Path | None = None) -> str:
    """Digest of the software that produced the record (not a container image)."""
    root = Path(__file__).resolve().parent.parent
    parts: list[bytes] = [f"cursor-agent-trace@{VERSION}".encode()]
    for path in (
        assemble_module or Path(__file__),
        hook_script or (root / "bin" / "cursor-agent-trace-hook.py"),
        root / "lib" / "model_identity.py",
        root / "lib" / "policy.py",
        root / "templates" / "agent-trace.policy.json",
        default_policy_bundle_path(),
    ):
        if path.is_file():
            parts.append(path.read_bytes())
            parts.append(str(path.name).encode())
    return _sha256_bytes(b"\n".join(parts))


def conversation_subject(events: list[dict[str, Any]], base: str) -> str:
    """SPIFFE/DID subject; optionally suffix with conversation id path segment."""
    if not _SUBJECT_RE.match(base or ""):
        raise MissingEvidence(
            f"subject {base!r} must be a SPIFFE URI or a DID "
            "(e.g. spiffe://local.cursor/agent/hooks)."
        )
    conv = None
    for event in events:
        for key in ("conversation_id", "session_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                conv = value.strip()
                break
        if conv:
            break
        payload = event.get("payload")
        if isinstance(payload, dict):
            for key in ("conversation_id", "session_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    conv = value.strip()
                    break
        if conv:
            break
    if not conv:
        return base
    # Keep subject a valid SPIFFE path: strip unsafe chars.
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in conv)[:120]
    if base.startswith("spiffe://"):
        return f"{base.rstrip('/')}/conversation/{safe}"
    return base


def build_level0_record(
    events: list[dict[str, Any]],
    *,
    subject: str | None = None,
    policy_bundle: bytes | None = None,
    policy_path: Path | None = None,
    data_class: str | None = None,
    workload: str | None = None,
    trail_path: Path | None = None,
    iat: int | None = None,
    model: ModelIdentity | None = None,
    appraisal_verifier: str | None = None,
) -> dict[str, Any]:
    """Build an unsigned software-observed Trust Record dict (no cnf/signature yet)."""
    if not events:
        raise MissingEvidence("no hook events — cannot assemble a TRACE from an empty trail")

    identity = model or resolve_model_identity(events, trail_path=trail_path)
    subj = conversation_subject(events, subject or os.environ.get("CURSOR_AGENT_TRACE_SUBJECT") or DEFAULT_SUBJECT)

    enforcement_mode = "declared"
    if policy_bundle is None:
        # Explicit CURSOR_AGENT_TRACE_POLICY pointing at a .txt legacy bundle.
        env_policy = os.environ.get("CURSOR_AGENT_TRACE_POLICY")
        if policy_path is None and env_policy:
            env_path = Path(env_policy)
            if env_path.is_file() and env_path.suffix.lower() in {".txt", ".md"}:
                policy_path = env_path
                enforcement_mode = "declared"
        if policy_path is not None:
            if not policy_path.is_file():
                raise MissingEvidence(f"policy bundle not found: {policy_path}")
            policy_bundle = policy_path.read_bytes()
        else:
            policy_bundle, enforcement_mode = resolve_policy_for_trace()
    if not policy_bundle:
        raise MissingEvidence("policy bundle bytes are empty")

    wl = workload or os.environ.get("CURSOR_AGENT_TRACE_WORKLOAD_DIGEST") or workload_digest()
    if not _DIGEST_RE.match(wl):
        raise MissingEvidence(f"workload digest must be sha256:/sha384:, got {wl!r}")

    transcript, call_count = build_tool_transcript(events)
    # Derived software measurement from workload + policy digests.
    runtime_measurement = _sha256_bytes(wl.encode() + b"\n" + _sha256_bytes(policy_bundle).encode())

    now = int(iat if iat is not None else time.time())
    source_event = None
    for event in reversed(events):
        for key in ("generation_id", "conversation_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                source_event = value.strip()
                break
        if source_event:
            break

    model_block: dict[str, Any] = {
        "provider": identity.provider,
        "model_id": identity.model_id,
    }
    if identity.version:
        model_block["version"] = identity.version

    record: dict[str, Any] = {
        "eat_profile": TRACE_PROFILE,
        "iat": now,
        "subject": subj,
        "model": model_block,
        "runtime": {
            "platform": "software-only",
            "measurement": runtime_measurement,
        },
        "policy": {
            "bundle_hash": _sha256_bytes(policy_bundle),
            "enforcement_mode": enforcement_mode,
        },
        "data_class": data_class
        or os.environ.get("CURSOR_AGENT_TRACE_DATA_CLASS")
        or DEFAULT_DATA_CLASS,
        "origin": {
            "kind": "third-party-control-plane",
            "producer": PRODUCER,
            "ingested_at": now,
        },
        "build_provenance": {
            "slsa_level": 0,
            "digest": wl,
            "builder": PRODUCER,
        },
        "appraisal": {
            "status": "none",
            "verifier": appraisal_verifier
            or os.environ.get("CURSOR_AGENT_TRACE_APPRAISAL_VERIFIER")
            or DEFAULT_APPRAISAL_VERIFIER,
        },
    }
    if source_event:
        record["origin"]["source_event_id"] = source_event
    if call_count:
        record["tool_transcript"] = {
            "hash": _sha256_bytes(transcript),
            "call_count": call_count,
        }
    if trail_path is not None and trail_path.is_file():
        record["references"] = [
            {
                "rel": "cursor-activity-jsonl",
                "id": trail_path.resolve().as_uri(),
                "resolver": PRODUCER,
                "digest": _sha256_file(trail_path),
            }
        ]
    return record


def sign_level0_record(
    record: dict[str, Any],
    *,
    key: Any | None = None,
) -> dict[str, Any]:
    """Sign with agentrust-trace. Uses TRACE_PRIVATE_KEY_PEM or ephemeral key."""
    try:
        from agentrust_trace import generate_key, load_signing_key, sign_record
    except ImportError as exc:  # pragma: no cover
        raise MissingEvidence(
            "agentrust-trace is required. Install with: "
            "pip install 'agentrust-trace>=0.9'"
        ) from exc

    if key is None:
        if os.environ.get("TRACE_PRIVATE_KEY_PEM"):
            key = load_signing_key()
        else:
            key = generate_key()
    return sign_record(dict(record), key)


def write_honesty_sidecar(
    path: Path,
    *,
    identity: ModelIdentity,
    record: dict[str, Any],
    trail_path: Path | None,
) -> Path:
    """Local-only notes (not part of the signed TRACE)."""
    if identity.from_operator_override:
        binding = "operator-declared-via-CURSOR_AGENT_TRACE_MODEL_ID"
    elif identity.inherited_from_parent:
        binding = "inherited-from-parent-conversation"
    else:
        binding = "asserted-from-cursor-hook-payload"
    sidecar = {
        "exporter": PRODUCER,
        "level_claim": "software-observed TRACE — software-only / platform-asserted / attestation none",
        "runtime_platform": "software-only",
        "attestation": "none",
        "model_binding": binding,
        "model_provider": identity.provider,
        "model_notes": list(identity.notes),
        "asserted_slugs": list(identity.asserted_slugs),
        "asserted_model_ids": list(identity.asserted_model_ids),
        "asserted_histogram": [
            {"model": name, "count": count} for name, count in identity.asserted_histogram
        ],
        "from_operator_override": identity.from_operator_override,
        "inherited_from_parent": identity.inherited_from_parent,
        "parent_conversation_id": identity.parent_conversation_id,
        "local_state_lookup": identity.local_state_lookup,
        "tee": False,
        "witness": False,
        "weights_attested": False,
        "source_trail": str(trail_path) if trail_path else None,
        "subject": record.get("subject"),
        "model_id_in_record": (record.get("model") or {}).get("model_id"),
    }
    # foo.trace.json → foo.honesty.json (local sidecar, not part of TRACE).
    name = path.name
    if name.endswith(".trace.json"):
        out = path.with_name(name[: -len(".trace.json")] + ".honesty.json")
    else:
        out = path.with_name(path.stem + ".honesty.json")
    out.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def assemble_and_sign_from_trail(
    trail: Path,
    *,
    out: Path | None = None,
    subject: str | None = None,
) -> tuple[Path, dict[str, Any], ModelIdentity]:
    """Load JSONL → TRACE record → sign → write ``*.trace.json`` (+ honesty sidecar)."""
    events = load_jsonl_events(trail)
    identity = resolve_model_identity(events, trail_path=trail)
    unsigned = build_level0_record(
        events,
        subject=subject,
        trail_path=trail,
        model=identity,
    )
    signed = sign_level0_record(unsigned)
    dest = out or trail.with_suffix(".trace.json")
    dest.write_text(json.dumps(signed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_honesty_sidecar(dest, identity=identity, record=signed, trail_path=trail)
    return dest, signed, identity
