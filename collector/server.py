#!/usr/bin/env python3
"""Org-side TRACE collector — HTTPS endpoint + Bearer token; S3 or local disk.

Laptops POST JSONL events / signed TRACE files here. AWS credentials live only
on this server (env: AWS_*, COLLECTOR_TOKEN, S3_BUCKET, PREFIX).

Zero required third-party deps (stdlib HTTP). Optional boto3 for S3; falls back
to local disk when S3 is unset or unavailable (CI / local-dev friendly).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

VERSION = "0.1.0"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def require_token() -> str:
    token = env("COLLECTOR_TOKEN")
    if not token:
        raise SystemExit("COLLECTOR_TOKEN is required")
    return token


def local_dir() -> Path:
    path = Path(env("LOCAL_DIR", "./data")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def s3_bucket() -> str | None:
    bucket = env("S3_BUCKET") or env("AWS_S3_BUCKET")
    return bucket or None


def s3_prefix() -> str:
    return env("PREFIX", "cursor-agent-trace").strip("/")


def safe_segment(value: str, fallback: str = "unknown") -> str:
    cleaned = SAFE_NAME.sub("_", (value or "").strip())[:180]
    return cleaned or fallback


class Store:
    """Write ingested payloads to S3 when configured, else local disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._s3 = None
        self._s3_error: str | None = None
        bucket = s3_bucket()
        if bucket:
            try:
                import boto3  # type: ignore

                self._s3 = boto3.client("s3")
            except Exception as exc:  # noqa: BLE001
                self._s3_error = str(exc)
                self._s3 = None

    @property
    def mode(self) -> str:
        if self._s3 is not None and s3_bucket():
            return "s3"
        return "local"

    def put_bytes(self, *, key: str, body: bytes, content_type: str) -> dict[str, Any]:
        bucket = s3_bucket()
        if self._s3 is not None and bucket:
            try:
                self._s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                )
                return {"ok": True, "backend": "s3", "bucket": bucket, "key": key}
            except Exception as exc:  # noqa: BLE001
                # Fall through to local so ingest never hard-fails the org pipe.
                local = self._write_local(key, body)
                local["s3_error"] = str(exc)
                local["fallback"] = True
                return local
        if self._s3_error and bucket:
            local = self._write_local(key, body)
            local["s3_error"] = self._s3_error
            local["fallback"] = True
            return local
        return self._write_local(key, body)

    def append_line(self, *, key: str, line: str) -> dict[str, Any]:
        """Append one JSONL line (local always; S3 uses put of accumulated file)."""
        with self._lock:
            local_path = local_dir() / key
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with local_path.open("a", encoding="utf-8") as fh:
                fh.write(line if line.endswith("\n") else line + "\n")
                fh.flush()
            result: dict[str, Any] = {
                "ok": True,
                "backend": "local",
                "path": str(local_path),
                "key": key,
            }
            bucket = s3_bucket()
            if self._s3 is not None and bucket:
                try:
                    body = local_path.read_bytes()
                    self._s3.put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=body,
                        ContentType="application/x-ndjson",
                    )
                    result["backend"] = "s3"
                    result["bucket"] = bucket
                    result["mirrored_local"] = str(local_path)
                except Exception as exc:  # noqa: BLE001
                    result["s3_error"] = str(exc)
                    result["fallback"] = True
            return result

    def _write_local(self, key: str, body: bytes) -> dict[str, Any]:
        path = local_dir() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {"ok": True, "backend": "local", "path": str(path), "key": key}


STORE = Store()
TOKEN = ""


def object_key(kind: str, conversation_id: str, filename: str | None = None) -> str:
    prefix = s3_prefix()
    conv = safe_segment(conversation_id)
    if filename:
        name = safe_segment(Path(filename).name)
    elif kind == "event":
        name = f"{conv}.jsonl"
    elif kind == "trace":
        name = f"{conv}.trace.json"
    elif kind == "honesty":
        name = f"{conv}.honesty.json"
    elif kind == "jsonl":
        name = f"{conv}.jsonl"
    else:
        name = f"{conv}.{safe_segment(kind, 'bin')}"
    return f"{prefix}/{name}"


def authorize(handler: BaseHTTPRequestHandler) -> bool:
    auth = handler.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    presented = auth[7:].strip()
    return bool(presented) and presented == TOKEN


def read_json_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return None
    if length > 32 * 1024 * 1024:
        raise ValueError("body too large (max 32MiB)")
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def ingest_one(payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest a single event / trace / honesty / jsonl object."""
    kind = str(payload.get("kind") or "event").strip().lower()
    conversation_id = str(
        payload.get("conversation_id")
        or payload.get("session_id")
        or "unknown"
    )
    filename = payload.get("filename")
    if filename is not None:
        filename = str(filename)

    if kind in {"event", "jsonl_line", "jsonl-line"}:
        # Single redacted hook event → append to conversation JSONL.
        event_body = payload.get("body")
        if event_body is None:
            event_body = payload.get("event") or payload.get("record")
        if event_body is None:
            raise ValueError("event ingest requires body/event/record")
        if isinstance(event_body, (dict, list)):
            line = json.dumps(event_body, ensure_ascii=False, default=str)
        else:
            line = str(event_body).rstrip("\n")
        key = object_key("event", conversation_id, filename)
        stored = STORE.append_line(key=key, line=line)
        return {"ok": True, "kind": "event", "conversation_id": conversation_id, **stored}

    if kind in {"trace", "honesty", "jsonl", "file"}:
        body = payload.get("body")
        if body is None:
            raise ValueError(f"{kind} ingest requires body")
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, ensure_ascii=False, default=str, indent=2).encode("utf-8")
            ctype = "application/json"
        elif isinstance(body, str):
            raw = body.encode("utf-8")
            ctype = "application/json" if kind != "jsonl" else "application/x-ndjson"
        else:
            raise ValueError("body must be object, array, or string")
        key = object_key(kind if kind != "file" else "jsonl", conversation_id, filename)
        if kind == "jsonl" and not (filename or "").endswith(".jsonl"):
            # Full JSONL replace/upload
            key = object_key("jsonl", conversation_id, filename)
        stored = STORE.put_bytes(key=key, body=raw, content_type=ctype)
        return {"ok": True, "kind": kind, "conversation_id": conversation_id, **stored}

    raise ValueError(f"unsupported kind: {kind!r} (use event|batch|trace|honesty|jsonl)")


def ingest(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept a single object or a batch (``kind: batch`` / ``events: [...]``)."""
    kind = str(payload.get("kind") or "").strip().lower()
    events = payload.get("events")

    # Batch: explicit kind, or bare events array (no top-level single body).
    if kind == "batch" or (isinstance(events, list) and kind not in {"event", "trace", "honesty", "jsonl", "file"}):
        if not isinstance(events, list):
            raise ValueError("batch ingest requires events: [...]")
        if len(events) > 500:
            raise ValueError("batch too large (max 500 events)")
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for idx, item in enumerate(events):
            if not isinstance(item, dict):
                errors.append({"index": idx, "error": "each event must be a JSON object"})
                continue
            # Nested items default to kind=event when omitted.
            nested = dict(item)
            if "kind" not in nested:
                nested["kind"] = "event"
            try:
                results.append(ingest_one(nested))
            except ValueError as exc:
                errors.append({"index": idx, "error": str(exc)})
        # HTTP-level success once the batch is processed. Per-item failures are
        # reported in ``errors``; clients only requeue on transport / 5xx failure
        # so we avoid duplicating accepted lines on retry.
        return {
            "ok": True,
            "kind": "batch",
            "accepted": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors,
        }

    # Single-object ingest (back-compat).
    if not kind:
        # Infer: body present → event; else error.
        if "body" in payload or "event" in payload or "record" in payload:
            payload = {**payload, "kind": "event"}
        else:
            raise ValueError("unsupported kind: '' (use event|batch|trace|honesty|jsonl)")
    return ingest_one(payload)


class Handler(BaseHTTPRequestHandler):
    server_version = f"cursor-agent-trace-collector/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/healthz", "/health", "/"}:
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "cursor-agent-trace-collector",
                    "version": VERSION,
                    "backend": STORE.mode,
                    "ts": utc_now_iso(),
                },
            )
            return
        send_json(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in {"/v1/ingest", "/ingest"}:
            send_json(self, 404, {"ok": False, "error": "not found"})
            return
        if not authorize(self):
            send_json(self, 401, {"ok": False, "error": "unauthorized"})
            return
        try:
            raw = read_json_body(self)
            if not isinstance(raw, dict):
                send_json(self, 400, {"ok": False, "error": "JSON object required"})
                return
            result = ingest(raw)
            send_json(self, 200, result)
        except ValueError as exc:
            send_json(self, 400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            send_json(self, 500, {"ok": False, "error": str(exc)})


def main() -> int:
    global TOKEN
    TOKEN = require_token()
    host = env("HOST", "0.0.0.0")
    port = int(env("PORT", "8787") or "8787")
    local_dir()  # ensure exists
    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(
        f"collector listening on http://{host}:{port} "
        f"backend={STORE.mode} prefix={s3_prefix()!r} local={local_dir()}\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nshutting down\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
