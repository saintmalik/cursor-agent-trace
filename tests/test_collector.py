"""Collector HTTP: bearer auth, single event, batch ingest, disk fallback."""

from __future__ import annotations

import importlib
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib import error, request

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def collector_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    data = tmp_path / "collector-data"
    data.mkdir()
    monkeypatch.setenv("COLLECTOR_TOKEN", "test-token-secret")
    monkeypatch.setenv("LOCAL_DIR", str(data))
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    return {"token": "test-token-secret", "data": data}


@pytest.fixture
def collector_server(collector_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Start a local ThreadingHTTPServer bound to 127.0.0.1:0 (ephemeral port)."""
    # Import fresh-ish so STORE picks up LOCAL_DIR / no S3.
    import sys

    mod_name = "collector_server_under_test"
    path = ROOT / "collector" / "server.py"
    # Load via path insertion so relative imports aren't an issue (stdlib only).
    sys.path.insert(0, str(ROOT / "collector"))
    try:
        if "server" in sys.modules:
            # Reload so Store() re-inits against our env.
            server = importlib.reload(sys.modules["server"])
        else:
            import server  # type: ignore

            server = server
    finally:
        pass

    server.TOKEN = collector_env["token"]
    server.STORE = server.Store()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base": f"http://127.0.0.1:{port}",
            "token": collector_env["token"],
            "data": collector_env["data"],
            "server": server,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base: str, token: str | None, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(f"{base}/v1/ingest", data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=5) as resp:  # noqa: S310 — localhost only
            body = resp.read().decode("utf-8")
            return int(resp.status), json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return int(exc.code), parsed


@pytest.mark.integration
def test_healthz(collector_server: dict[str, Any]):
    base = collector_server["base"]
    with request.urlopen(f"{base}/healthz", timeout=5) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    assert data["ok"] is True
    assert data["service"] == "cursor-agent-trace-collector"
    assert data["backend"] == "local"


@pytest.mark.integration
def test_auth_bearer_required(collector_server: dict[str, Any]):
    status, body = _post(collector_server["base"], None, {"kind": "event", "body": {"x": 1}})
    assert status == 401
    assert body.get("ok") is False

    status, body = _post(
        collector_server["base"],
        "wrong-token",
        {"kind": "event", "conversation_id": "c1", "body": {"x": 1}},
    )
    assert status == 401


@pytest.mark.integration
def test_single_event_ingest(collector_server: dict[str, Any]):
    status, body = _post(
        collector_server["base"],
        collector_server["token"],
        {
            "kind": "event",
            "conversation_id": "conv-single",
            "body": {"hook_event_name": "stop", "n": 1},
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert body["kind"] == "event"
    assert body["backend"] == "local"
    path = Path(body["path"])
    assert path.is_file()
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(line)["n"] == 1


@pytest.mark.integration
def test_batch_ingest(collector_server: dict[str, Any]):
    status, body = _post(
        collector_server["base"],
        collector_server["token"],
        {
            "kind": "batch",
            "events": [
                {"conversation_id": "conv-batch", "body": {"n": 1}},
                {"conversation_id": "conv-batch", "body": {"n": 2}},
                {
                    "kind": "trace",
                    "conversation_id": "conv-batch",
                    "body": {"eat_profile": "demo"},
                },
            ],
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert body["kind"] == "batch"
    assert body["accepted"] == 3
    assert body["failed"] == 0
    data = collector_server["data"]
    jsonl = list(data.rglob("conv-batch.jsonl"))
    assert jsonl, "expected conversation JSONL on disk"
    lines = [json.loads(l) for l in jsonl[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    traces = list(data.rglob("conv-batch.trace.json"))
    assert traces


@pytest.mark.integration
def test_disk_fallback_when_s3_put_fails(
    collector_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """Store falls through to local disk when the S3 client raises."""
    import sys

    sys.path.insert(0, str(ROOT / "collector"))
    if "server" in sys.modules:
        server = importlib.reload(sys.modules["server"])
    else:
        import server  # type: ignore

        server = server

    monkeypatch.setenv("S3_BUCKET", "fake-bucket")
    monkeypatch.setenv("LOCAL_DIR", str(collector_env["data"]))

    class Boom:
        def put_object(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("simulated S3 failure")

    store = server.Store()
    store._s3 = Boom()  # type: ignore[assignment]
    result = store.put_bytes(
        key="cursor-agent-trace/fallback.trace.json",
        body=b'{"ok":true}',
        content_type="application/json",
    )
    assert result["ok"] is True
    assert result["backend"] == "local"
    assert result.get("fallback") is True
    assert "s3_error" in result
    path = Path(result["path"])
    assert path.is_file()
    assert path.read_bytes() == b'{"ok":true}'
