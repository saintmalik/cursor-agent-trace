"""Local HTTP viewer for Cursor Agent TRACE JSONL trails.

Modes:
  * Single trail: ``python -m lib view path/to/conversation.jsonl``
  * Library:      ``python -m lib view`` or ``python -m lib view --library``

Library mode scans configured trail dirs (and optional S3) and lets you pick
a conversation. Deep links: ``?id=<conversation_id>`` or ``/t/<id>``.

Level-0 TRACE conformance runs server-side when a sibling ``.trace.json``
exists (``/api/bundle``, on-demand ``/api/verify``).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import library as _library
from .model_identity import resolve_model_identity

ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = ROOT / "viewer"

_CHECK_LINE_RE = re.compile(
    r"^\s*(TR-[A-Z]+)\s+(PASS|FAIL|SKIP)\s+(.*)$"
)


def sibling_paths(jsonl: Path) -> dict[str, Path | None]:
    """Resolve optional sibling artifacts next to a ``*.jsonl`` trail."""
    path = Path(jsonl).expanduser().resolve()
    if path.suffix.lower() != ".jsonl":
        raise ValueError(f"expected a .jsonl trail, got: {path}")
    stem = path.stem
    parent = path.parent
    trace = parent / f"{stem}.trace.json"
    honesty = parent / f"{stem}.honesty.json"
    return {
        "jsonl": path,
        "trace": trace if trace.is_file() else None,
        "honesty": honesty if honesty.is_file() else None,
    }


def _read_json(path: Path | None) -> Any | None:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
        if isinstance(row, dict):
            events.append(row)
    return events


def parse_verify_output(text: str) -> list[dict[str, str]]:
    """Parse ``trace-tests`` Level-0 report lines into structured checks."""
    checks: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        match = _CHECK_LINE_RE.match(line)
        if not match:
            continue
        checks.append(
            {
                "id": match.group(1),
                "result": match.group(2),
                "message": match.group(3).strip(),
            }
        )
    return checks


def soft_verify(trace_path: Path | None) -> dict[str, Any]:
    """Best-effort Level-0 check. Never blocks the viewer on missing tooling.

    When no sibling ``.trace.json`` exists, returns status ``n/a`` with detail
    ``no signed TRACE yet`` — never a fake PASS.
    """
    if trace_path is None or not trace_path.is_file():
        return {
            "status": "n/a",
            "detail": "no signed TRACE yet",
            "checks": [],
            "summary": None,
        }

    try:
        record = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "FAIL",
            "detail": f"unreadable trace: {exc}",
            "checks": [],
            "summary": None,
        }

    if not isinstance(record, dict):
        return {
            "status": "FAIL",
            "detail": "trace is not a JSON object",
            "checks": [],
            "summary": None,
        }

    has_sig = bool(record.get("signature")) and isinstance(record.get("cnf"), dict)
    exe = shutil.which("trace-tests")
    if not has_sig:
        return {
            "status": "FAIL",
            "detail": "missing signature/cnf — record is not signed (different from a policy DENY on a tool call)",
            "checks": [],
            "summary": None,
        }
    if not exe:
        return {
            "status": "signed",
            "detail": (
                "signature present, but trace-tests CLI not found on PATH — "
                "install with: pip install agentrust-trace-tests"
            ),
            "checks": [],
            "summary": None,
        }

    try:
        proc = subprocess.run(
            [exe, "verify", "--record", str(trace_path), "--level", "0"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if has_sig:
            return {
                "status": "signed",
                "detail": f"verify skipped: {exc}",
                "checks": [],
                "summary": None,
            }
        return {
            "status": "FAIL",
            "detail": f"verify error: {exc}",
            "checks": [],
            "summary": None,
        }

    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    checks = parse_verify_output(combined)
    summary_line = None
    for line in reversed(combined.splitlines()):
        if line.strip().startswith("Result:"):
            summary_line = line.strip()
            break

    if proc.returncode == 0:
        return {
            "status": "PASS",
            "detail": summary_line or "trace-tests verify --level 0",
            "checks": checks,
            "summary": summary_line,
        }

    detail = summary_line
    if not detail:
        lines = combined.splitlines()
        detail = lines[-1].strip() if lines else f"exit {proc.returncode}"
    return {
        "status": "FAIL",
        "detail": detail,
        "checks": checks,
        "summary": summary_line,
    }


def build_bundle(jsonl: Path) -> dict[str, Any]:
    """Load trail + siblings into one API payload for the viewer."""
    siblings = sibling_paths(jsonl)
    assert siblings["jsonl"] is not None
    events = _read_jsonl_events(siblings["jsonl"])
    trace = _read_json(siblings["trace"])
    honesty = _read_json(siblings["honesty"])
    verify = soft_verify(siblings["trace"])
    identity = resolve_model_identity(events, trail_path=siblings["jsonl"])
    asserted_models = [
        {"model": name, "count": count} for name, count in identity.asserted_histogram
    ]
    return {
        "id": siblings["jsonl"].stem,
        "source": {
            "jsonl": str(siblings["jsonl"]),
            "trace": str(siblings["trace"]) if siblings["trace"] else None,
            "honesty": str(siblings["honesty"]) if siblings["honesty"] else None,
        },
        "events": events,
        "trace": trace,
        "honesty": honesty,
        "verify": verify,
        "model_identity": {
            "provider": identity.provider,
            "model_id": identity.model_id,
            "inherited_from_parent": identity.inherited_from_parent,
            "parent_conversation_id": identity.parent_conversation_id,
            "from_operator_override": identity.from_operator_override,
            "notes": list(identity.notes),
            "asserted_slugs": list(identity.asserted_slugs),
            "asserted_model_ids": list(identity.asserted_model_ids),
            "local_state_lookup": identity.local_state_lookup,
        },
        "asserted_models": asserted_models,
    }


class _ViewerState:
    def __init__(
        self,
        *,
        viewer_dir: Path,
        mode: str,
        jsonl: Path | None,
        bundle: dict[str, Any] | None,
        extra_dirs: list[Path] | None = None,
        include_s3: bool = True,
    ) -> None:
        self.viewer_dir = viewer_dir
        self.mode = mode  # single | library
        self.jsonl = jsonl
        self.bundle = bundle
        self.extra_dirs = extra_dirs or []
        self.include_s3 = include_s3
        self._bundle_cache: dict[str, dict[str, Any]] = {}
        if bundle and jsonl is not None:
            self._bundle_cache[jsonl.stem] = bundle

    def library_payload(self) -> dict[str, Any]:
        return _library.build_library_index(
            include_s3=self.include_s3,
            extra_dirs=self.extra_dirs,
        )

    def resolve_jsonl(self, conversation_id: str, source: str = "auto") -> Path:
        return _library.resolve_trail_path(
            conversation_id,
            source=source,
            extra_dirs=self.extra_dirs,
        )

    def bundle_for(self, conversation_id: str, source: str = "auto") -> dict[str, Any]:
        cache_key = f"{source}:{conversation_id}"
        if cache_key in self._bundle_cache:
            return self._bundle_cache[cache_key]
        # Prefer already-loaded single trail.
        if self.jsonl is not None and self.jsonl.stem == conversation_id and self.bundle:
            return self.bundle
        path = self.resolve_jsonl(conversation_id, source=source)
        bundle = build_bundle(path)
        self._bundle_cache[cache_key] = bundle
        self._bundle_cache[path.stem] = bundle
        self.jsonl = path
        self.bundle = bundle
        return bundle


def _make_handler(state: _ViewerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _serve_static(self, rel: str) -> None:
            target = (state.viewer_dir / rel).resolve()
            if not str(target).startswith(str(state.viewer_dir.resolve())):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if not target.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            data = target.read_bytes()
            ctype, _ = mimetypes.guess_type(str(target))
            if not ctype:
                ctype = "application/octet-stream"
            if ctype.startswith("text/") or ctype in {
                "application/javascript",
                "application/json",
            }:
                ctype = f"{ctype}; charset=utf-8"
            self._send(200, data, ctype)

        def _query(self) -> dict[str, list[str]]:
            return parse_qs(urlparse(self.path).query)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            query = parse_qs(parsed.query)

            if path == "/api/health":
                self._send_json(
                    200,
                    {"ok": True, "mode": state.mode, "library": state.mode == "library"},
                )
                return

            if path == "/api/library":
                try:
                    payload = state.library_payload()
                    payload["mode"] = state.mode
                    payload["active_id"] = state.jsonl.stem if state.jsonl else None
                    self._send_json(200, payload)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if path == "/api/bundle":
                conv_id = (query.get("id") or [None])[0]
                source = (query.get("source") or ["auto"])[0]
                try:
                    if conv_id:
                        bundle = state.bundle_for(conv_id, source=source or "auto")
                    elif state.bundle is not None:
                        bundle = state.bundle
                    else:
                        self._send_json(
                            404,
                            {
                                "error": "no trail selected",
                                "hint": "pass ?id=<conversation_id> or open /t/<id>",
                            },
                        )
                        return
                    self._send_json(200, bundle)
                except FileNotFoundError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": str(exc)})
                return

            if path == "/api/verify":
                conv_id = (query.get("id") or [None])[0]
                source = (query.get("source") or ["auto"])[0]
                try:
                    if conv_id:
                        bundle = state.bundle_for(conv_id, source=source or "auto")
                        jsonl = Path(bundle["source"]["jsonl"])
                    elif state.jsonl is not None:
                        jsonl = state.jsonl
                    else:
                        self._send_json(
                            404,
                            {
                                "status": "FAIL",
                                "detail": "no trail selected",
                                "checks": [],
                                "error": "no trail selected",
                            },
                        )
                        return
                    siblings = sibling_paths(jsonl)
                    result = soft_verify(siblings["trace"])
                    # Keep every cache entry for this trail in sync with the re-check.
                    if state.bundle is not None:
                        src = state.bundle.get("source") or {}
                        if src.get("jsonl") and Path(src["jsonl"]) == jsonl:
                            state.bundle["verify"] = result
                    for key, cached in list(state._bundle_cache.items()):
                        src = (cached or {}).get("source") or {}
                        if src.get("jsonl") and Path(src["jsonl"]) == jsonl:
                            cached["verify"] = result
                    self._send_json(200, result)
                except FileNotFoundError as exc:
                    self._send_json(
                        404,
                        {
                            "status": "FAIL",
                            "detail": str(exc),
                            "checks": [],
                            "error": str(exc),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(
                        500,
                        {
                            "status": "FAIL",
                            "detail": str(exc),
                            "checks": [],
                            "error": str(exc),
                        },
                    )
                return

            # Deep link /t/<conversation_id> → SPA shell (client loads via ?id=).
            if path.startswith("/t/"):
                self._serve_static("index.html")
                return

            if path in {"/", "/index.html"}:
                self._serve_static("index.html")
                return

            if path.startswith("/"):
                self._serve_static(path.lstrip("/"))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def serve(
    jsonl: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    library: bool = False,
    include_s3: bool = True,
    extra_dirs: list[Path] | None = None,
) -> int:
    _library.load_viewer_config()

    if not VIEWER_DIR.is_dir():
        print(f"error: viewer assets missing: {VIEWER_DIR}", file=sys.stderr)
        return 2

    extras = list(extra_dirs or [])
    bundle: dict[str, Any] | None = None
    resolved: Path | None = None
    mode = "library"

    if jsonl is not None and not library:
        try:
            siblings = sibling_paths(jsonl)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        assert siblings["jsonl"] is not None
        if not siblings["jsonl"].is_file():
            print(f"error: trail not found: {siblings['jsonl']}", file=sys.stderr)
            return 2
        try:
            bundle = build_bundle(siblings["jsonl"])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: failed to load trail: {exc}", file=sys.stderr)
            return 2
        resolved = siblings["jsonl"]
        mode = "single"
        extras.append(resolved.parent)

    state = _ViewerState(
        viewer_dir=VIEWER_DIR,
        mode=mode,
        jsonl=resolved,
        bundle=bundle,
        extra_dirs=extras,
        include_s3=include_s3,
    )
    handler = _make_handler(state)

    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"error: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 2

    url = f"http://{host}:{httpd.server_address[1]}/"
    lines = ["Cursor Agent TRACE viewer", f"  mode:    {mode}"]
    if resolved is not None and bundle is not None:
        verify = bundle["verify"]
        check_n = len(verify.get("checks") or [])
        lines.extend(
            [
                f"  trail:   {bundle['source']['jsonl']}",
                f"  trace:   {bundle['source']['trace'] or '(none)'}",
                f"  honesty: {bundle['source']['honesty'] or '(none)'}",
                f"  verify:  {verify['status']} — {verify['detail']}"
                + (f" ({check_n} checks)" if check_n else ""),
            ]
        )
    else:
        dirs = _library.configured_trail_dirs(extra=extras)
        lines.append(f"  library: {len(dirs)} dir(s)")
        for d in dirs[:5]:
            lines.append(f"           {d}")
        cfg = _library.s3_config()
        if cfg.enabled:
            lines.append(f"  s3:      s3://{cfg.bucket}/{cfg.prefix}/")
        else:
            lines.append(f"  s3:      {cfg.detail}")
        lines.append("  open:    pick a trail in the left rail, or /t/<id>")
    lines.extend([f"  url:     {url}", "Ctrl+C to stop."])
    print("\n".join(lines), flush=True)

    open_url = url
    if resolved is not None:
        open_url = f"{url}?id={resolved.stem}"

    if open_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.35), webbrowser.open(open_url)),
            daemon=True,
        ).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0


def add_parser_clean(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "view",
        help="Open a local web UI for trails (library or single JSONL)",
        description=__doc__,
    )
    p.add_argument(
        "trail",
        type=Path,
        nargs="?",
        default=None,
        help="Optional path to *.jsonl (omit for library mode)",
    )
    p.add_argument(
        "--library",
        action="store_true",
        help="Force library browser even if a trail path is given",
    )
    p.add_argument(
        "--no-s3",
        action="store_true",
        help="Do not list/fetch S3 trails",
    )
    p.add_argument(
        "--dir",
        action="append",
        type=Path,
        default=[],
        help="Extra trail directory to scan (repeatable)",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port (default: 8765)",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="Print the URL without opening a browser",
    )
    p.set_defaults(_handler=run_from_args)


def run_from_args(args: argparse.Namespace) -> int:
    return serve(
        args.trail,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        library=bool(args.library) or args.trail is None,
        include_s3=not bool(args.no_s3),
        extra_dirs=list(args.dir or []),
    )


def build_arg_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog or "view", description=__doc__)
    parser.add_argument("trail", type=Path, nargs="?", default=None)
    parser.add_argument("--library", action="store_true")
    parser.add_argument("--no-s3", action="store_true")
    parser.add_argument("--dir", action="append", type=Path, default=[])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser(prog="view.py")
    args = parser.parse_args(argv)
    return run_from_args(args)


if __name__ == "__main__":
    raise SystemExit(main())
