#!/usr/bin/env python3
"""Cursor Agent TRACE hook — JSONL activity trail + signed TRACE on stop.

Uses the official Cursor hooks stdin/stdout JSON protocol only.
On every event: append a redacted record to a local JSONL trail (optional S3
or org collector batch). On ``stop`` / ``sessionEnd``: assemble and sign a
software-observed TRACE Trust Record with ``agentrust-trace`` (software-only,
attestation none, never invents weights), then optionally upload to S3 or
the org collector (Bearer token only — no AWS keys on the laptop).

Org collector pipe: events are buffered on disk and POSTed as batches to
``/v1/ingest``. Network failures land in a durable outbox
(``…/agent-trace/outbox/``) and are replayed opportunistically with backoff.
Local JSONL remains the source of truth; collector outages never block Cursor.

Allow/deny: ``preToolUse`` / ``beforeReadFile`` / ``beforeShellExecution`` (and
related) evaluate ``agent-trace.policy.json``. Mode ``enforce`` returns
``permission: deny`` on match; ``observe`` only logs. Hook crashes and
collector outages still fail-open so a bug cannot brick Cursor.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import assemble as _assemble  # noqa: E402
from lib import outbox as _outbox  # noqa: E402
from lib import policy as _policy  # noqa: E402
from lib import tab_trail as _tab_trail  # noqa: E402

VERSION = _assemble.VERSION

_CONFIG_LOADED = False

SENSITIVE_KEYS = frozenset(
    {
        "prompt",
        "text",
        "content",
        "output",
        "tool_output",
        "result_json",
        "summary",
        "agent_message",
        "error_message",
        "edits",
        "tool_input",
        "attachments",
        "user_email",
    }
)

PERMISSION_EVENTS = frozenset(
    {
        "preToolUse",
        "beforeShellExecution",
        "beforeMCPExecution",
        "beforeReadFile",
        "beforeTabFileRead",
        "subagentStart",
    }
)


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        out[key] = value
    return out


# Happy-path defaults — installer also writes these into agent-trace.env.
# Process env overrides config; config overrides these baked values.
_BAKED_DEFAULTS: dict[str, str] = {
    "CURSOR_AGENT_TRACE_REDACT": "safe",
    "CURSOR_AGENT_TRACE_SIGN": "1",
    "CURSOR_AGENT_TRACE_S3": "0",
    # Tab events roll into tab.jsonl (not one UUID file per Tab read).
    "CURSOR_AGENT_TRACE_TAB_MODE": "consolidated",
    # Collector batch / retry (only used when COLLECTOR_URL is set).
    "COLLECTOR_BATCH_SIZE": str(_outbox.DEFAULT_BATCH_SIZE),
    "COLLECTOR_FLUSH_INTERVAL_SEC": str(int(_outbox.DEFAULT_FLUSH_INTERVAL_SEC)),
    "COLLECTOR_RETRY_BASE_SEC": str(int(_outbox.DEFAULT_RETRY_BASE_SEC)),
    "COLLECTOR_RETRY_MAX_SEC": str(int(_outbox.DEFAULT_RETRY_MAX_SEC)),
    "COLLECTOR_RETRY_MAX_ATTEMPTS": str(_outbox.DEFAULT_RETRY_MAX_ATTEMPTS),
    "COLLECTOR_TIMEOUT": "5",
}


def apply_baked_defaults() -> list[str]:
    """Fill still-missing keys with plug-and-play defaults (no shell exports)."""
    applied: list[str] = []
    for key, value in _BAKED_DEFAULTS.items():
        if key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def load_config(*, force: bool = False) -> list[str]:
    """Load durable config, then bake defaults. Process env always wins.

    Precedence for each key:
      1. Process environment already set (operator / Cursor-injected)
      2. CURSOR_AGENT_TRACE_CONFIG / CURSOR_ACTIVITY_CONFIG
      3. $CURSOR_PROJECT_DIR/.cursor/agent-trace.env
      4. $CURSOR_PROJECT_DIR/.cursor/activity-export.env (compat)
      5. ~/.cursor/agent-trace.env
      6. ~/.cursor/activity-export.env (compat)
      7. Baked defaults (REDACT=safe, SIGN=1, S3=0, collector batch/retry)
    """
    global _CONFIG_LOADED
    if _CONFIG_LOADED and not force:
        return []
    _CONFIG_LOADED = True

    candidates: list[Path] = []
    for env_key in ("CURSOR_AGENT_TRACE_CONFIG", "CURSOR_ACTIVITY_CONFIG"):
        explicit = os.environ.get(env_key)
        if explicit:
            candidates.append(Path(explicit).expanduser())
    project = os.environ.get("CURSOR_PROJECT_DIR")
    if project:
        pdir = Path(project) / ".cursor"
        candidates.append(pdir / "agent-trace.env")
        candidates.append(pdir / "activity-export.env")
        candidates.append(pdir / "activity-export.config")
    candidates.append(Path.home() / ".cursor" / "agent-trace.env")
    candidates.append(Path.home() / ".cursor" / "activity-export.env")

    applied: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = str(path)
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        newly = 0
        for key, value in _parse_env_file(path).items():
            if key in os.environ:
                continue
            os.environ[key] = value
            newly += 1
        if newly:
            applied.append(resolved)
    baked = apply_baked_defaults()
    if baked:
        applied.append("baked-defaults:" + ",".join(baked))
    return applied


load_config()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def s3_enabled() -> bool:
    return env_bool("CURSOR_AGENT_TRACE_S3", False) or env_bool("CURSOR_ACTIVITY_S3", False)


def collector_url() -> str | None:
    """Org collector base URL. Off unless configured (no AWS keys on laptop)."""
    raw = (
        os.environ.get("COLLECTOR_URL")
        or os.environ.get("CURSOR_AGENT_TRACE_COLLECTOR_URL")
        or ""
    ).strip().rstrip("/")
    return raw or None


def collector_token() -> str:
    return (
        os.environ.get("COLLECTOR_TOKEN")
        or os.environ.get("CURSOR_AGENT_TRACE_COLLECTOR_TOKEN")
        or ""
    ).strip()


def redact_mode() -> str:
    mode = (
        os.environ.get("CURSOR_AGENT_TRACE_REDACT")
        or os.environ.get("CURSOR_ACTIVITY_REDACT")
        or "safe"
    ).strip().lower()
    if mode in {"off", "none", "full", "raw"}:
        return "off"
    return "safe"


def summarize_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"present": False}
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
        return {
            "present": True,
            "type": "string",
            "chars": len(value),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, (dict, list)):
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        raw = blob.encode("utf-8", errors="replace")
        return {
            "present": True,
            "type": type(value).__name__,
            "chars": len(blob),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    blob = str(value).encode("utf-8", errors="replace")
    return {
        "present": True,
        "type": type(value).__name__,
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
    }


def redact_payload(obj: Any, mode: str) -> Any:
    if mode == "off":
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in SENSITIVE_KEYS:
                out[key] = {"_redacted": True, **summarize_value(value)}
            else:
                out[key] = redact_payload(value, mode)
        return out
    if isinstance(obj, list):
        return [redact_payload(item, mode) for item in obj]
    return obj


def trail_dir() -> Path:
    explicit = os.environ.get("CURSOR_AGENT_TRACE_DIR") or os.environ.get("CURSOR_ACTIVITY_DIR")
    if explicit:
        path = Path(explicit).expanduser()
    else:
        project = os.environ.get("CURSOR_PROJECT_DIR") or os.getcwd()
        path = Path(project) / ".cursor" / "agent-trace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def conversation_key(payload: dict[str, Any]) -> str:
    """Grouping key for trail filename / collector.

    Tab-scoped events in consolidated mode share ``tab`` (or ``tab-YYYYMMDD``);
    Agent / Task / subagent keep the Cursor conversation UUID.
    """
    return _tab_trail.conversation_key_for_trail(payload)


def trail_path_for(payload: dict[str, Any]) -> Path:
    key = conversation_key(payload)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:180]
    return trail_dir() / f"{safe}.jsonl"


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def maybe_upload_s3(path: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """Optional S3 upload. Fail-open."""
    if not s3_enabled():
        return None
    bucket = (
        os.environ.get("CURSOR_AGENT_TRACE_S3_BUCKET")
        or os.environ.get("CURSOR_ACTIVITY_S3_BUCKET")
        or os.environ.get("AWS_S3_BUCKET")
    )
    if not bucket:
        return {"ok": False, "error": "CURSOR_AGENT_TRACE_S3_BUCKET / CURSOR_ACTIVITY_S3_BUCKET not set"}

    prefix = (
        os.environ.get("CURSOR_AGENT_TRACE_S3_PREFIX")
        or os.environ.get("CURSOR_ACTIVITY_S3_PREFIX")
        or "cursor-agent-trace"
    ).strip("/")
    key = f"{prefix}/{path.name}"
    mode = (
        os.environ.get("CURSOR_AGENT_TRACE_S3_MODE")
        or os.environ.get("CURSOR_ACTIVITY_S3_MODE")
        or "file"
    ).strip().lower()

    try:
        import boto3  # type: ignore
    except Exception as exc:  # noqa: BLE001 — optional dep
        return upload_via_aws_cli(path, bucket, key, mode, record, fallback_error=str(exc))

    try:
        client = boto3.client("s3")
        if mode == "event":
            event_key = (
                f"{prefix}/{path.stem}/"
                f"{int(time.time() * 1000)}-{record.get('hook_event_name', 'event')}.json"
            )
            client.put_object(
                Bucket=bucket,
                Key=event_key,
                Body=json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            return {"ok": True, "bucket": bucket, "key": event_key, "mode": "event"}
        client.upload_file(str(path), bucket, key)
        return {"ok": True, "bucket": bucket, "key": key, "mode": "file"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def upload_via_aws_cli(
    path: Path,
    bucket: str,
    key: str,
    mode: str,
    record: dict[str, Any],
    fallback_error: str,
) -> dict[str, Any]:
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("aws"):
        return {
            "ok": False,
            "error": f"boto3 unavailable ({fallback_error}) and aws CLI not found",
        }
    try:
        if mode == "event":
            prefix = key.rsplit("/", 1)[0]
            event_key = (
                f"{prefix}/{path.stem}/"
                f"{int(time.time() * 1000)}-{record.get('hook_event_name', 'event')}.json"
            )
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tmp:
                json.dump(record, tmp, ensure_ascii=False, default=str)
                tmp_path = tmp.name
            try:
                subprocess.run(
                    ["aws", "s3", "cp", tmp_path, f"s3://{bucket}/{event_key}"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            return {"ok": True, "bucket": bucket, "key": event_key, "mode": "event", "via": "aws-cli"}
        subprocess.run(
            ["aws", "s3", "cp", str(path), f"s3://{bucket}/{key}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return {"ok": True, "bucket": bucket, "key": key, "mode": "file", "via": "aws-cli"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def outbox_root() -> Path:
    """Durable pending + outbox live next to local JSONL trails."""
    root = trail_dir()
    (root / "outbox").mkdir(parents=True, exist_ok=True)
    (root / "pending").mkdir(parents=True, exist_ok=True)
    return root


def collector_poster() -> _outbox.PostFn | None:
    """HTTP poster for the org collector, or None if collector is off / misconfigured."""
    base = collector_url()
    if not base:
        return None
    token = collector_token()
    if not token:
        return None
    return _outbox.make_http_poster(
        base_url=base,
        token=token,
        user_agent=f"cursor-agent-trace-hook/{VERSION}",
    )


def log_collector_meta(trail: Path, info: dict[str, Any]) -> None:
    meta_path = trail.with_suffix(".collector.jsonl")
    try:
        append_jsonl(meta_path, {"ts": utc_now_iso(), "trail": str(trail), **info})
    except Exception:  # noqa: BLE001
        pass


def enqueue_and_flush_collector(
    *,
    trail: Path,
    conversation_id: str,
    record: dict[str, Any],
    force_flush: bool = False,
) -> None:
    """Buffer event, flush batch when due, replay outbox. Fail-open."""
    if not collector_url():
        return
    if not collector_token():
        log_collector_meta(
            trail,
            {"ok": False, "kind": "event", "error": "COLLECTOR_URL set but COLLECTOR_TOKEN missing"},
        )
        return
    post = collector_poster()
    if post is None:
        return
    try:
        box = _outbox.Outbox(outbox_root())
        box.enqueue_event(
            conversation_id=conversation_id,
            body=record,
            filename=trail.name,
        )
        info = box.flush(post, force=force_flush)
        log_collector_meta(trail, {"kind": "batch" if info.get("flushed") else "buffer", **info})
    except Exception as exc:  # noqa: BLE001 — never block Cursor
        log_collector_meta(trail, {"ok": False, "kind": "batch", "error": str(exc)})


def post_artifact_collector(
    *,
    trail: Path,
    kind: str,
    conversation_id: str,
    body: Any,
    filename: str | None = None,
) -> None:
    """POST trace/honesty (or queue on failure) + opportunistic outbox replay."""
    if not collector_url():
        return
    post = collector_poster()
    if post is None:
        log_collector_meta(
            trail,
            {"ok": False, "kind": kind, "error": "COLLECTOR_URL set but COLLECTOR_TOKEN missing"},
        )
        return
    payload: dict[str, Any] = {
        "kind": kind,
        "conversation_id": conversation_id,
        "body": body,
    }
    if filename:
        payload["filename"] = filename
    try:
        box = _outbox.Outbox(outbox_root())
        # Drain pending events first so TRACE lands after its events.
        flush_info = box.flush(post, force=True)
        log_collector_meta(trail, {"kind": "batch", "phase": "pre-artifact", **flush_info})
        info = box.post_or_queue(payload, post)
        log_collector_meta(trail, {"kind": kind, **info})
        # Another opportunistic replay after artifact post.
        replay = box.replay_outbox(post)
        if replay.get("attempted"):
            log_collector_meta(trail, {"kind": "replay", **replay})
    except Exception as exc:  # noqa: BLE001
        log_collector_meta(trail, {"ok": False, "kind": kind, "error": str(exc)})


def response_for(
    event_name: str,
    *,
    decision: _policy.Decision | None = None,
) -> dict[str, Any]:
    if event_name in PERMISSION_EVENTS:
        if decision is not None and decision.blocked:
            out: dict[str, Any] = {
                "permission": "deny",
                "user_message": decision.reason or "Blocked by agent-trace policy",
                "agent_message": (
                    f"Denied by agent-trace policy"
                    + (f" ({decision.matched_rule})" if decision.matched_rule else "")
                ),
            }
            return out
        return {"permission": "allow"}
    if event_name == "beforeSubmitPrompt":
        return {"continue": True}
    return {}


def build_record(
    raw: dict[str, Any],
    *,
    decision: _policy.Decision | None = None,
    policy: _policy.Policy | None = None,
) -> dict[str, Any]:
    mode = redact_mode()
    event_name = raw.get("hook_event_name") or "unknown"
    record: dict[str, Any] = {
        "ts": utc_now_iso(),
        "exporter": f"cursor-agent-trace@{VERSION}",
        "redact": mode,
        "hook_event_name": event_name,
        "conversation_id": raw.get("conversation_id") or raw.get("session_id"),
        "generation_id": raw.get("generation_id"),
        "model": raw.get("model"),
        "model_id": raw.get("model_id"),
        "cursor_version": raw.get("cursor_version") or os.environ.get("CURSOR_VERSION"),
        "workspace_roots": raw.get("workspace_roots"),
        "cursor_project_dir": os.environ.get("CURSOR_PROJECT_DIR"),
        "payload": redact_payload(raw, mode),
    }
    if decision is not None and policy is not None:
        record["policy_decision"] = _policy.decision_to_record(decision, policy)
    return record


def maybe_sign_trace(payload: dict[str, Any], trail: Path) -> Path | None:
    """On stop/sessionEnd, assemble a signed TRACE from the JSONL trail.

    Tab trails stay JSONL-only: consolidating many Tab reads into one Level-0
    subject would invent a fake conversation. Agent UUID trails still sign.
    """
    if not env_bool("CURSOR_AGENT_TRACE_SIGN", True):
        return None
    if _tab_trail.is_tab_scoped(payload) or _tab_trail.is_tab_trail_stem(trail.stem):
        return None
    event = payload.get("hook_event_name")
    if event not in {"sessionEnd", "stop"}:
        return None
    try:
        dest, _signed, identity = _assemble.assemble_and_sign_from_trail(trail)
    except Exception as exc:  # noqa: BLE001 — fail open; log for Hooks channel
        sys.stderr.write(f"cursor-agent-trace sign error: {exc}\n")
        sys.stderr.write(traceback.format_exc())
        return None
    if identity.from_operator_override:
        sys.stderr.write(
            f"cursor-agent-trace: model_id={identity.model_id!r} from "
            "CURSOR_AGENT_TRACE_MODEL_ID (operator-declared lab override, "
            "not observed from hooks)\n"
        )
    elif identity.model_id.strip().lower() in {"default", "unknown", ""}:
        sys.stderr.write(
            "cursor-agent-trace: model_id recorded as Cursor-asserted "
            f"{identity.model_id!r} (hooks sent no concrete model; "
            "not fabricated)\n"
        )
    if s3_enabled():
        maybe_upload_s3(dest, {"hook_event_name": "signed-trace", **payload})
        honesty = dest.with_name(dest.name.replace(".trace.json", ".honesty.json"))
        if honesty.is_file():
            maybe_upload_s3(honesty, {"hook_event_name": "honesty-sidecar", **payload})

    # Org collector: upload signed TRACE (+ honesty) — no AWS keys on laptop.
    if collector_url():
        conv = conversation_key(payload)
        try:
            trace_body = json.loads(dest.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log_collector_meta(
                trail,
                {"ok": False, "kind": "trace", "error": f"read trace: {exc}"},
            )
            return dest
        post_artifact_collector(
            trail=trail,
            kind="trace",
            conversation_id=conv,
            body=trace_body,
            filename=dest.name,
        )
        honesty = dest.with_name(dest.name.replace(".trace.json", ".honesty.json"))
        if honesty.is_file():
            try:
                honesty_body = json.loads(honesty.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log_collector_meta(
                    trail,
                    {"ok": False, "kind": "honesty", "error": f"read honesty: {exc}"},
                )
            else:
                post_artifact_collector(
                    trail=trail,
                    kind="honesty",
                    conversation_id=conv,
                    body=honesty_body,
                    filename=honesty.name,
                )
    return dest


def main() -> int:
    try:
        stdin = sys.stdin.read()
        if not stdin.strip():
            print("{}")
            return 0
        raw = json.loads(stdin)
        if not isinstance(raw, dict):
            print("{}")
            return 0

        event_name = str(raw.get("hook_event_name") or "unknown")
        pol = _policy.load_policy()
        decision = _policy.evaluate(raw, pol)
        record = build_record(raw, decision=decision, policy=pol)
        path = trail_path_for(raw)
        # Always record the decision (allow / deny / would_deny) before responding.
        append_jsonl(path, record)

        s3_info = maybe_upload_s3(path, record)
        if s3_info is not None:
            meta_path = path.with_suffix(".s3.jsonl")
            append_jsonl(meta_path, {"ts": utc_now_iso(), "trail": str(path), **s3_info})

        # Org collector: buffer events → batch POST (outbox on failure); fail-open.
        event_name_for_flush = str(raw.get("hook_event_name") or "")
        force_flush = event_name_for_flush in {"stop", "sessionEnd"}
        enqueue_and_flush_collector(
            trail=path,
            conversation_id=conversation_key(raw),
            record=record,
            force_flush=force_flush,
        )

        maybe_sign_trace(raw, path)

        print(json.dumps(response_for(event_name, decision=decision)))
        return 0
    except Exception:  # noqa: BLE001
        # Fail-open on unexpected errors so a hook bug cannot brick Cursor.
        sys.stderr.write("cursor-agent-trace-hook error:\n")
        sys.stderr.write(traceback.format_exc())
        print(json.dumps({"permission": "allow", "continue": True}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
