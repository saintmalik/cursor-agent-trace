"""Trail library: discover local + S3 Cursor Agent TRACE conversations.

Used by ``python -m lib view`` (library mode) to list conversations across
configured trail directories and optional org S3 prefixes. Reuses the same env
dialect as the hook / installer (``CURSOR_AGENT_TRACE_*``, ``AWS_*``) — no
second config language.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import tab_trail as _tab_trail

SAFE_STEM = re.compile(r"[^A-Za-z0-9._\-]+")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            out[key] = value
    return out


def load_viewer_config() -> list[str]:
    """Load agent-trace.env into process env (process keys win). Idempotent-ish."""
    candidates: list[Path] = []
    for env_key in ("CURSOR_AGENT_TRACE_CONFIG", "CURSOR_ACTIVITY_CONFIG"):
        explicit = os.environ.get(env_key)
        if explicit:
            candidates.append(Path(explicit).expanduser())
    project = os.environ.get("CURSOR_PROJECT_DIR") or os.getcwd()
    pdir = Path(project) / ".cursor"
    candidates.append(pdir / "agent-trace.env")
    candidates.append(pdir / "activity-export.env")
    candidates.append(Path.home() / ".cursor" / "agent-trace.env")
    candidates.append(Path.home() / ".cursor" / "activity-export.env")

    applied: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = str(path.resolve()) if path.exists() else str(path)
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
            applied.append(str(path))
    return applied


def safe_stem(conversation_id: str) -> str:
    cleaned = SAFE_STEM.sub("_", (conversation_id or "").strip())[:180]
    return cleaned or "unknown"


def configured_trail_dirs(*, extra: list[Path] | None = None) -> list[Path]:
    """Ordered unique directories that may hold ``*.jsonl`` trails."""
    dirs: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        dirs.append(resolved)

    for env_key in ("CURSOR_AGENT_TRACE_DIR", "CURSOR_ACTIVITY_DIR"):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            add(Path(raw))

    project = os.environ.get("CURSOR_PROJECT_DIR") or os.getcwd()
    add(Path(project) / ".cursor" / "agent-trace")
    add(Path.cwd() / ".cursor" / "agent-trace")
    add(Path.home() / ".cursor" / "agent-trace")

    if extra:
        for path in extra:
            add(path)

    return dirs


@dataclass
class TrailEntry:
    """One conversation in the library index."""

    id: str
    source: str  # local | s3
    jsonl: str | None = None
    has_trace: bool = False
    has_honesty: bool = False
    mtime: float | None = None
    mtime_iso: str | None = None
    event_count: int | None = None
    bytes: int | None = None
    user: str | None = None
    workspace: str | None = None
    subject: str | None = None
    model_id: str | None = None
    kind: str | None = None  # tab | subagent | parent | agent
    parent_id: str | None = None  # parent conversation when this trail is a subagent
    verify_status: str | None = None  # lazy: None until computed
    s3_bucket: str | None = None
    s3_key: str | None = None
    dir: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso_from_mtime(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cheap_event_count(path: Path, *, max_bytes: int = 2_000_000) -> int | None:
    """Count non-empty lines without full JSON parse. Caps huge files."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        # Approximate via sampling first max_bytes.
        try:
            chunk = path.read_bytes()[:max_bytes]
        except OSError:
            return None
        lines = chunk.count(b"\n")
        if not chunk.endswith(b"\n"):
            lines += 1
        # Scale roughly.
        return max(1, int(lines * (size / max(len(chunk), 1))))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if line.strip())


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _walk_parent_ids(obj: Any, out: list[str], *, depth: int = 0) -> None:
    """Collect parent_conversation_id-like strings without a full model_identity import cycle."""
    if depth > 6 or obj is None:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            lk = str(key).lower().replace("-", "_")
            if lk in {
                "parent_conversation_id",
                "parentconversationid",
                "parent_composer_id",
                "parentcomposerid",
            }:
                text = _as_str(value)
                if text:
                    out.append(text)
            else:
                _walk_parent_ids(value, out, depth=depth + 1)
    elif isinstance(obj, list):
        for item in obj[:32]:
            _walk_parent_ids(item, out, depth=depth + 1)


def _peek_meta(jsonl: Path, honesty: Path | None) -> dict[str, Any]:
    """Best-effort user / workspace / subject / parent linkage from honesty or JSONL."""
    meta: dict[str, Any] = {
        "user": None,
        "workspace": None,
        "subject": None,
        "model_id": None,
        "parent_id": None,
        "is_subagent": False,
        "spawns_subagents": False,
        "self_id": jsonl.stem,
    }
    if honesty is not None and honesty.is_file():
        try:
            data = json.loads(honesty.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            meta["subject"] = data.get("subject") if isinstance(data.get("subject"), str) else None
            mid = data.get("model_id_in_record")
            if isinstance(mid, str) and mid.strip():
                meta["model_id"] = mid.strip()
            parent = _as_str(data.get("parent_conversation_id"))
            if parent and parent != meta["self_id"]:
                meta["parent_id"] = parent
                if data.get("inherited_from_parent") is True:
                    meta["is_subagent"] = True

    self_ids: set[str] = {meta["self_id"]}
    try:
        with jsonl.open("r", encoding="utf-8") as fh:
            for _ in range(48):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                for key in ("conversation_id", "session_id"):
                    cid = _as_str(row.get(key))
                    if cid:
                        self_ids.add(cid)
                event_name = str(row.get("hook_event_name") or "")
                if event_name == "subagentStart":
                    meta["spawns_subagents"] = True
                if meta["workspace"] is None:
                    roots = row.get("workspace_roots")
                    if isinstance(roots, list) and roots:
                        first = roots[0]
                        if isinstance(first, str):
                            meta["workspace"] = first
                    project = row.get("cursor_project_dir")
                    if meta["workspace"] is None and isinstance(project, str):
                        meta["workspace"] = project
                if meta["user"] is None:
                    payload = row.get("payload")
                    if isinstance(payload, dict):
                        email = payload.get("user_email")
                        meta["user"] = _display_user(email)
                if meta["model_id"] is None:
                    mid = row.get("model_id") or row.get("model")
                    if isinstance(mid, str) and mid.strip():
                        meta["model_id"] = mid.strip()
                found: list[str] = []
                _walk_parent_ids(row, found)
                for cand in found:
                    if cand not in self_ids:
                        meta["parent_id"] = cand
                        meta["is_subagent"] = True
                if (
                    meta["user"]
                    and meta["workspace"]
                    and meta["model_id"]
                    and (meta["parent_id"] or meta["spawns_subagents"])
                ):
                    break
    except OSError:
        pass
    return meta


def _classify_kind(stem: str, meta: dict[str, Any]) -> str:
    if _tab_trail.is_tab_trail_stem(stem):
        return "tab"
    if meta.get("is_subagent") and meta.get("parent_id"):
        return "subagent"
    if meta.get("spawns_subagents"):
        return "parent"
    return "agent"


def _apply_parent_backrefs(entries: list[TrailEntry]) -> None:
    """Promote agent → parent when another trail lists it as parent_id."""
    children_of: set[str] = set()
    for entry in entries:
        if entry.parent_id:
            children_of.add(entry.parent_id)
    for entry in entries:
        if entry.kind in {"tab", "subagent"}:
            continue
        if entry.id in children_of:
            entry.kind = "parent"


def _display_user(value: Any) -> str | None:
    """Surface user/email when present; respect redaction stubs."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict) and value.get("_redacted") is True:
        return "(redacted)"
    return None


def scan_local_dir(directory: Path) -> list[TrailEntry]:
    """Index ``*.jsonl`` conversations in one trail directory."""
    if not directory.is_dir():
        return []
    entries: list[TrailEntry] = []
    try:
        files = sorted(directory.glob("*.jsonl"))
    except OSError:
        return []
    for path in files:
        # Skip nested outbox/pending dumps if any land as jsonl.
        if path.parent.name in {"outbox", "pending"}:
            continue
        # Skip hook side-channel logs (foo.collector.jsonl / foo.s3.jsonl).
        if path.name.endswith(".collector.jsonl") or path.name.endswith(".s3.jsonl"):
            continue
        stem = path.stem
        trace = directory / f"{stem}.trace.json"
        honesty = directory / f"{stem}.honesty.json"
        try:
            st = path.stat()
            mtime = st.st_mtime
            size = st.st_size
        except OSError:
            mtime = None
            size = None
        meta = _peek_meta(path, honesty if honesty.is_file() else None)
        kind = _classify_kind(stem, meta)
        if kind == "tab" and not meta.get("model_id"):
            meta["model_id"] = "tab"
        entries.append(
            TrailEntry(
                id=stem,
                source="local",
                jsonl=str(path),
                has_trace=trace.is_file(),
                has_honesty=honesty.is_file(),
                mtime=mtime,
                mtime_iso=_iso_from_mtime(mtime),
                event_count=_cheap_event_count(path),
                bytes=size,
                user=meta["user"],
                workspace=meta["workspace"],
                subject=meta["subject"],
                model_id=meta["model_id"],
                kind=kind,
                parent_id=meta.get("parent_id") if kind != "tab" else None,
                dir=str(directory),
            )
        )
    _apply_parent_backrefs(entries)
    return entries


def scan_local_trails(
    dirs: list[Path] | None = None,
    *,
    extra: list[Path] | None = None,
) -> list[TrailEntry]:
    """Scan all configured local trail dirs (dedupe by conversation id, newest wins)."""
    directories = dirs if dirs is not None else configured_trail_dirs(extra=extra)
    by_id: dict[str, TrailEntry] = {}
    for directory in directories:
        for entry in scan_local_dir(directory):
            prev = by_id.get(entry.id)
            if prev is None:
                by_id[entry.id] = entry
                continue
            prev_m = prev.mtime or 0.0
            cur_m = entry.mtime or 0.0
            if cur_m >= prev_m:
                by_id[entry.id] = entry
    return sorted(
        by_id.values(),
        key=lambda e: e.mtime or 0.0,
        reverse=True,
    )


@dataclass
class S3Config:
    enabled: bool
    bucket: str | None = None
    prefix: str = "cursor-agent-trace"
    region: str | None = None
    detail: str = ""


def s3_config() -> S3Config:
    """Read the same S3 env dialect as the hook / collector."""
    bucket = (
        os.environ.get("CURSOR_AGENT_TRACE_S3_BUCKET")
        or os.environ.get("CURSOR_ACTIVITY_S3_BUCKET")
        or os.environ.get("AWS_S3_BUCKET")
        or os.environ.get("S3_BUCKET")
        or ""
    ).strip() or None
    prefix = (
        os.environ.get("CURSOR_AGENT_TRACE_S3_PREFIX")
        or os.environ.get("CURSOR_ACTIVITY_S3_PREFIX")
        or os.environ.get("PREFIX")
        or "cursor-agent-trace"
    ).strip("/")
    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip() or None
    # Listing is useful whenever a bucket is configured, even if laptop S3 upload is off.
    if not bucket:
        return S3Config(
            enabled=False,
            prefix=prefix,
            region=region,
            detail="S3 not configured (set CURSOR_AGENT_TRACE_S3_BUCKET or S3_BUCKET)",
        )
    return S3Config(enabled=True, bucket=bucket, prefix=prefix, region=region, detail="ok")


def _s3_client(cfg: S3Config) -> Any:
    import boto3  # type: ignore

    kwargs: dict[str, Any] = {}
    if cfg.region:
        kwargs["region_name"] = cfg.region
    return boto3.client("s3", **kwargs)


def scan_s3_trails(*, max_keys: int = 2000) -> dict[str, Any]:
    """List conversation objects under the org S3 prefix. Soft-fail on errors."""
    cfg = s3_config()
    if not cfg.enabled or not cfg.bucket:
        return {
            "ok": False,
            "configured": False,
            "detail": cfg.detail,
            "trails": [],
            "bucket": None,
            "prefix": cfg.prefix,
        }

    try:
        client = _s3_client(cfg)
    except Exception as exc:  # noqa: BLE001 — optional dep / creds
        return {
            "ok": False,
            "configured": True,
            "detail": f"S3 client unavailable: {exc}",
            "trails": [],
            "bucket": cfg.bucket,
            "prefix": cfg.prefix,
        }

    prefix = f"{cfg.prefix}/" if cfg.prefix else ""
    grouped: dict[str, dict[str, Any]] = {}
    try:
        token: str | None = None
        listed = 0
        while listed < max_keys:
            kwargs: dict[str, Any] = {
                "Bucket": cfg.bucket,
                "Prefix": prefix,
                "MaxKeys": min(1000, max_keys - listed),
            }
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents") or []:
                key = obj.get("Key") or ""
                name = key.rsplit("/", 1)[-1]
                listed += 1
                stem: str | None = None
                kind: str | None = None
                if name.endswith(".trace.json"):
                    stem = name[: -len(".trace.json")]
                    kind = "trace"
                elif name.endswith(".honesty.json"):
                    stem = name[: -len(".honesty.json")]
                    kind = "honesty"
                elif name.endswith(".jsonl"):
                    stem = name[: -len(".jsonl")]
                    kind = "jsonl"
                else:
                    continue
                bucket_entry = grouped.setdefault(
                    stem,
                    {
                        "id": stem,
                        "source": "s3",
                        "has_trace": False,
                        "has_honesty": False,
                        "has_jsonl": False,
                        "s3_bucket": cfg.bucket,
                        "s3_key": None,
                        "mtime": None,
                        "bytes": None,
                        "kind": "tab" if _tab_trail.is_tab_trail_stem(stem) else "agent",
                    },
                )
                last_mod = obj.get("LastModified")
                mtime = last_mod.timestamp() if hasattr(last_mod, "timestamp") else None
                size = obj.get("Size")
                if kind == "jsonl":
                    bucket_entry["has_jsonl"] = True
                    bucket_entry["s3_key"] = key
                    bucket_entry["bytes"] = size
                    if mtime is not None and (
                        bucket_entry["mtime"] is None or mtime >= (bucket_entry["mtime"] or 0)
                    ):
                        bucket_entry["mtime"] = mtime
                elif kind == "trace":
                    bucket_entry["has_trace"] = True
                    if mtime is not None and bucket_entry["mtime"] is None:
                        bucket_entry["mtime"] = mtime
                elif kind == "honesty":
                    bucket_entry["has_honesty"] = True
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "configured": True,
            "detail": f"S3 list failed: {exc}",
            "trails": [],
            "bucket": cfg.bucket,
            "prefix": cfg.prefix,
        }

    trails: list[dict[str, Any]] = []
    for stem, raw in grouped.items():
        if not raw.get("has_jsonl") and not raw.get("has_trace"):
            continue
        mtime = raw.get("mtime")
        trails.append(
            TrailEntry(
                id=stem,
                source="s3",
                jsonl=None,
                has_trace=bool(raw.get("has_trace")),
                has_honesty=bool(raw.get("has_honesty")),
                mtime=mtime,
                mtime_iso=_iso_from_mtime(mtime),
                event_count=None,
                bytes=raw.get("bytes"),
                model_id="tab" if raw.get("kind") == "tab" else None,
                kind=raw.get("kind") if isinstance(raw.get("kind"), str) else None,
                s3_bucket=cfg.bucket,
                s3_key=raw.get("s3_key") or f"{prefix}{stem}.jsonl",
            ).to_dict()
        )
    trails.sort(key=lambda t: t.get("mtime") or 0.0, reverse=True)
    return {
        "ok": True,
        "configured": True,
        "detail": f"listed {len(trails)} conversation(s) from s3://{cfg.bucket}/{prefix}",
        "trails": trails,
        "bucket": cfg.bucket,
        "prefix": cfg.prefix,
    }


def viewer_cache_dir() -> Path:
    explicit = (os.environ.get("CURSOR_AGENT_TRACE_VIEWER_CACHE") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    else:
        base = configured_trail_dirs()
        root = base[0] if base else Path.cwd() / ".cursor" / "agent-trace"
        path = root / ".viewer-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_s3_trail(conversation_id: str) -> Path:
    """Download jsonl (+ siblings when present) into the viewer cache. Raises on hard fail."""
    cfg = s3_config()
    if not cfg.enabled or not cfg.bucket:
        raise FileNotFoundError(cfg.detail or "S3 not configured")

    stem = safe_stem(conversation_id)
    cache = viewer_cache_dir() / "s3" / (cfg.bucket or "bucket") / cfg.prefix
    cache.mkdir(parents=True, exist_ok=True)
    client = _s3_client(cfg)
    prefix = f"{cfg.prefix}/" if cfg.prefix else ""

    downloaded_jsonl: Path | None = None
    for suffix, label in (
        (".jsonl", "jsonl"),
        (".trace.json", "trace"),
        (".honesty.json", "honesty"),
    ):
        key = f"{prefix}{stem}{suffix}"
        dest = cache / f"{stem}{suffix}"
        try:
            client.download_file(cfg.bucket, key, str(dest))
        except Exception as exc:  # noqa: BLE001
            if label == "jsonl":
                # Try listing for exact key if stem sanitization differed.
                raise FileNotFoundError(f"S3 object not found: s3://{cfg.bucket}/{key} ({exc})") from exc
            continue
        if label == "jsonl":
            downloaded_jsonl = dest

    if downloaded_jsonl is None or not downloaded_jsonl.is_file():
        raise FileNotFoundError(f"No JSONL for conversation {conversation_id!r} in S3")
    return downloaded_jsonl


def build_library_index(
    *,
    include_s3: bool = True,
    extra_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Full library payload for ``/api/library``."""
    load_viewer_config()
    dirs = configured_trail_dirs(extra=extra_dirs)
    local = [e.to_dict() for e in scan_local_trails(dirs)]
    s3: dict[str, Any]
    if include_s3:
        s3 = scan_s3_trails()
    else:
        cfg = s3_config()
        s3 = {
            "ok": False,
            "configured": cfg.enabled,
            "detail": "S3 listing skipped",
            "trails": [],
            "bucket": cfg.bucket,
            "prefix": cfg.prefix,
        }
    return {
        "dirs": [str(d) for d in dirs],
        "local": local,
        "s3": s3,
        "counts": {
            "local": len(local),
            "s3": len(s3.get("trails") or []),
        },
    }


def resolve_trail_path(
    conversation_id: str,
    *,
    source: str = "auto",
    extra_dirs: list[Path] | None = None,
) -> Path:
    """Resolve a conversation id to a local JSONL path (may download from S3)."""
    stem = safe_stem(conversation_id)
    source = (source or "auto").strip().lower()

    if source in {"auto", "local"}:
        for directory in configured_trail_dirs(extra=extra_dirs):
            candidate = directory / f"{stem}.jsonl"
            if candidate.is_file():
                return candidate.resolve()
        # Also accept raw id path if caller passed a filename stem that differs.
        for directory in configured_trail_dirs(extra=extra_dirs):
            for path in directory.glob("*.jsonl"):
                if path.stem == conversation_id or path.stem == stem:
                    return path.resolve()

    if source in {"auto", "s3"}:
        return fetch_s3_trail(conversation_id).resolve()

    raise FileNotFoundError(f"trail not found: {conversation_id!r} (source={source})")
