"""Durable collector batch buffer + offline outbox (fail-open).

Cursor hooks are separate processes, so pending events and failed POSTs live
on disk under ``<trail_dir>/pending/`` and ``<trail_dir>/outbox/``.

Local JSONL trails remain the source of truth; this module only manages the
optional org-collector pipe.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Defaults written by installer into agent-trace.env (process env wins).
DEFAULT_BATCH_SIZE = 10
DEFAULT_FLUSH_INTERVAL_SEC = 2.0
DEFAULT_RETRY_BASE_SEC = 1.0
DEFAULT_RETRY_MAX_SEC = 60.0
DEFAULT_RETRY_MAX_ATTEMPTS = 0  # 0 = unlimited


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def batch_size() -> int:
    return max(1, _env_int("COLLECTOR_BATCH_SIZE", DEFAULT_BATCH_SIZE))


def flush_interval_sec() -> float:
    return max(0.0, _env_float("COLLECTOR_FLUSH_INTERVAL_SEC", DEFAULT_FLUSH_INTERVAL_SEC))


def retry_base_sec() -> float:
    return max(0.1, _env_float("COLLECTOR_RETRY_BASE_SEC", DEFAULT_RETRY_BASE_SEC))


def retry_max_sec() -> float:
    return max(retry_base_sec(), _env_float("COLLECTOR_RETRY_MAX_SEC", DEFAULT_RETRY_MAX_SEC))


def retry_max_attempts() -> int:
    return max(0, _env_int("COLLECTOR_RETRY_MAX_ATTEMPTS", DEFAULT_RETRY_MAX_ATTEMPTS))


@dataclass
class PostResult:
    ok: bool
    status: int | None = None
    error: str | None = None
    response: dict[str, Any] | None = None
    url: str | None = None


PostFn = Callable[[dict[str, Any]], PostResult]


class Outbox:
    """Pending event buffer + durable failed-batch outbox."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending_dir = root / "pending"
        self.outbox_dir = root / "outbox"
        self.buffer_path = self.pending_dir / "buffer.jsonl"
        self.state_path = self.pending_dir / "state.json"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def enqueue_event(
        self,
        *,
        conversation_id: str,
        body: Any,
        filename: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "kind": "event",
            "conversation_id": conversation_id,
            "body": body,
        }
        if filename:
            item["filename"] = filename
        with self._locked(self.buffer_path, create=True) as fh:
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def pending_count(self) -> int:
        if not self.buffer_path.is_file():
            return 0
        with self._locked(self.buffer_path, create=False) as fh:
            if fh is None:
                return 0
            fh.seek(0)
            return sum(1 for line in fh if line.strip())

    def should_flush(self, *, force: bool = False) -> bool:
        if force:
            return self.pending_count() > 0
        count = self.pending_count()
        if count <= 0:
            return False
        if count >= batch_size():
            return True
        interval = flush_interval_sec()
        if interval <= 0:
            return True
        last = self._last_flush_monotonic()
        if last is None:
            # First events: flush when interval elapsed since buffer birth,
            # or immediately if count already meets size (handled above).
            # Use file mtime of buffer as birth proxy.
            try:
                age = time.time() - self.buffer_path.stat().st_mtime
            except OSError:
                return False
            return age >= interval
        return (time.monotonic() - last) >= interval

    def take_pending(self) -> list[dict[str, Any]]:
        """Atomically drain the pending buffer into a list of event items."""
        with self._locked(self.buffer_path, create=True) as fh:
            fh.seek(0)
            text = fh.read()
            events: list[dict[str, Any]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
            fh.seek(0)
            fh.truncate(0)
            fh.flush()
            os.fsync(fh.fileno())
        self._set_last_flush_monotonic(time.monotonic())
        return events

    def write_outbox(self, payload: dict[str, Any], *, error: str | None = None) -> Path:
        entry_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
        path = self.outbox_dir / f"{entry_id}.json"
        # Defer first replay so the same flush() call does not immediately re-POST.
        entry = {
            "id": entry_id,
            "created_at": utc_now_iso(),
            "attempts": 0,
            "next_attempt_at": time.time() + retry_base_sec(),
            "last_error": error,
            "payload": payload,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def list_outbox(self) -> list[Path]:
        if not self.outbox_dir.is_dir():
            return []
        return sorted(p for p in self.outbox_dir.glob("*.json") if p.is_file())

    def outbox_count(self) -> int:
        return len(self.list_outbox())

    def flush(
        self,
        post: PostFn,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Flush pending buffer as a batch if due; always try opportunistic outbox replay."""
        result: dict[str, Any] = {
            "flushed": False,
            "pending_before": self.pending_count(),
            "outbox_before": self.outbox_count(),
        }
        if self.should_flush(force=force):
            events = self.take_pending()
            if events:
                payload = {"kind": "batch", "events": events}
                posted = post(payload)
                result["flushed"] = True
                result["batch_size"] = len(events)
                result["post"] = _post_meta(posted)
                if not posted.ok:
                    path = self.write_outbox(payload, error=posted.error)
                    result["queued"] = str(path)
                    result["ok"] = False
                    result["error"] = posted.error
                else:
                    result["ok"] = True
            else:
                result["ok"] = True
        else:
            result["ok"] = True

        replay = self.replay_outbox(post)
        result["replay"] = replay
        result["pending_after"] = self.pending_count()
        result["outbox_after"] = self.outbox_count()
        return result

    def post_or_queue(self, payload: dict[str, Any], post: PostFn) -> dict[str, Any]:
        """POST a single payload (trace/honesty/batch); on failure write outbox."""
        posted = post(payload)
        meta = _post_meta(posted)
        if posted.ok:
            return {"ok": True, **meta}
        path = self.write_outbox(payload, error=posted.error)
        return {"ok": False, "queued": str(path), "error": posted.error, **meta}

    def replay_outbox(self, post: PostFn, *, limit: int = 32) -> dict[str, Any]:
        """Opportunistically replay due outbox entries (backoff respected)."""
        now = time.time()
        attempted = 0
        succeeded = 0
        deferred = 0
        failed = 0
        dropped = 0
        max_attempts = retry_max_attempts()

        for path in self.list_outbox()[:limit]:
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                # Corrupt entry — leave for operator inspection.
                failed += 1
                continue
            if not isinstance(entry, dict):
                failed += 1
                continue
            next_at = float(entry.get("next_attempt_at") or 0)
            if next_at > now:
                deferred += 1
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                failed += 1
                continue
            attempted += 1
            posted = post(payload)
            if posted.ok:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                succeeded += 1
                continue
            attempts = int(entry.get("attempts") or 0) + 1
            if max_attempts and attempts >= max_attempts:
                # Keep file but mark exhausted so operators can inspect.
                entry["attempts"] = attempts
                entry["exhausted"] = True
                entry["last_error"] = posted.error
                entry["next_attempt_at"] = now + retry_max_sec()
                self._rewrite_entry(path, entry)
                dropped += 1
                continue
            delay = min(retry_max_sec(), retry_base_sec() * (2 ** max(0, attempts - 1)))
            entry["attempts"] = attempts
            entry["last_error"] = posted.error
            entry["last_attempt_at"] = utc_now_iso()
            entry["next_attempt_at"] = now + delay
            self._rewrite_entry(path, entry)
            failed += 1

        return {
            "attempted": attempted,
            "succeeded": succeeded,
            "deferred": deferred,
            "failed": failed,
            "dropped": dropped,
            "remaining": self.outbox_count(),
        }

    def _rewrite_entry(self, path: Path, entry: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _last_flush_monotonic(self) -> float | None:
        state = self._read_state()
        val = state.get("last_flush_monotonic")
        if isinstance(val, (int, float)):
            return float(val)
        return None

    def _set_last_flush_monotonic(self, value: float) -> None:
        state = self._read_state()
        state["last_flush_monotonic"] = value
        state["last_flush_at"] = utc_now_iso()
        self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def _locked(self, path: Path, *, create: bool):
        return _FileLock(path, create=create)


class _FileLock:
    """Context manager: exclusive flock around a text file."""

    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = path
        self.create = create
        self._fh: Any = None

    def __enter__(self):
        if not self.create and not self.path.is_file():
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self._fh

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def _post_meta(posted: PostResult) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": posted.ok}
    if posted.status is not None:
        out["status"] = posted.status
    if posted.error:
        out["error"] = posted.error
    if posted.url:
        out["url"] = posted.url
    if posted.response:
        # Keep meta small — only top-level ok/kind/accepted from collector.
        for key in ("kind", "accepted", "backend", "results"):
            if key in posted.response:
                out[key] = posted.response[key]
    return out


def make_http_poster(
    *,
    base_url: str,
    token: str,
    timeout: float | None = None,
    user_agent: str = "cursor-agent-trace-hook",
) -> PostFn:
    """Build a PostFn that POSTs JSON to ``{base}/v1/ingest``."""

    url = f"{base_url.rstrip('/')}/v1/ingest"
    if timeout is None:
        timeout = _env_float("COLLECTOR_TIMEOUT", 5.0)

    def post(payload: dict[str, Any]) -> PostResult:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            import urllib.request

            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": user_agent,
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                if not isinstance(parsed, dict):
                    parsed = {"raw": parsed}
                status = getattr(resp, "status", 200)
                ok = 200 <= int(status) < 300 and parsed.get("ok", True) is not False
                return PostResult(ok=ok, status=int(status), response=parsed, url=url)
        except Exception as exc:  # noqa: BLE001 — fail-open
            return PostResult(ok=False, error=str(exc), url=url)

    return post
