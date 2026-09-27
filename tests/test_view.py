"""Tests for the local Agent Trace viewer helpers + library browser."""

from __future__ import annotations

import json
from pathlib import Path

from lib import library, view


def test_sibling_paths_finds_optional_artifacts(tmp_path: Path) -> None:
    trail = tmp_path / "conv-abc.jsonl"
    trail.write_text('{"hook_event_name":"sessionEnd"}\n', encoding="utf-8")
    (tmp_path / "conv-abc.trace.json").write_text('{"subject":"x"}\n', encoding="utf-8")
    (tmp_path / "conv-abc.honesty.json").write_text('{"subject":"x"}\n', encoding="utf-8")

    siblings = view.sibling_paths(trail)
    assert siblings["jsonl"] == trail.resolve()
    assert siblings["trace"] == (tmp_path / "conv-abc.trace.json").resolve()
    assert siblings["honesty"] == (tmp_path / "conv-abc.honesty.json").resolve()


def test_sibling_paths_missing_optional(tmp_path: Path) -> None:
    trail = tmp_path / "lonely.jsonl"
    trail.write_text("{}\n", encoding="utf-8")
    siblings = view.sibling_paths(trail)
    assert siblings["jsonl"] == trail.resolve()
    assert siblings["trace"] is None
    assert siblings["honesty"] is None


def test_soft_verify_no_trace_is_honest_na() -> None:
    result = view.soft_verify(None)
    assert result["status"] == "n/a"
    assert "no signed TRACE yet" in result["detail"]
    assert result["checks"] == []


def test_parse_verify_output_extracts_check_lines() -> None:
    sample = """
TRACE Conformance Report -- Level 0

  TR-ENV  PASS        eat_profile sentinel matches
  TR-SIG  PASS        Ed25519 signature verified
  TR-POL  SKIP        policy.policy_uri not present (optional)

Result: PASS  (3 checks, 1 skipped)
"""
    checks = view.parse_verify_output(sample)
    assert [c["id"] for c in checks] == ["TR-ENV", "TR-SIG", "TR-POL"]
    assert checks[0]["result"] == "PASS"
    assert checks[2]["result"] == "SKIP"


def test_build_bundle_loads_events_and_verify_shape(tmp_path: Path) -> None:
    trail = tmp_path / "demo.jsonl"
    events = [
        {
            "ts": "2026-01-01T00:00:00Z",
            "hook_event_name": "preToolUse",
            "conversation_id": "demo",
            "model": "default",
            "model_id": "grok-4.5",
            "payload": {"tool_name": "Shell", "model": "default"},
            "policy_decision": {"mode": "enforce", "decision": "allow"},
        }
    ]
    trail.write_text(
        "".join(json.dumps(e) + "\n" for e in events),
        encoding="utf-8",
    )
    (tmp_path / "demo.trace.json").write_text(
        json.dumps(
            {
                "subject": "spiffe://local.cursor/agent/hooks/conversation/demo",
                "model": {"provider": "cursor-asserted", "model_id": "grok-4.5"},
                "policy": {"enforcement_mode": "enforce"},
                "tool_transcript": {"call_count": 1},
                "signature": "abc",
                "cnf": {"jwk": {"kty": "OKP"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "demo.honesty.json").write_text(
        json.dumps({"subject": "spiffe://local.cursor/agent/hooks/conversation/demo"})
        + "\n",
        encoding="utf-8",
    )

    bundle = view.build_bundle(trail)
    assert len(bundle["events"]) == 1
    assert bundle["trace"]["tool_transcript"]["call_count"] == 1
    assert bundle["honesty"]["subject"].endswith("/demo")
    assert bundle["verify"]["status"] in {"PASS", "FAIL", "signed", "n/a"}
    assert bundle["source"]["jsonl"].endswith("demo.jsonl")
    assert bundle["source"]["trace"]
    assert bundle["source"]["honesty"]
    assert bundle["model_identity"]["model_id"] == "grok-4.5"
    assert any(row["model"] == "default" for row in bundle["asserted_models"])
    assert any(row["model"] == "grok-4.5" for row in bundle["asserted_models"])


def test_build_bundle_without_trace_verify_na(tmp_path: Path) -> None:
    trail = tmp_path / "plain.jsonl"
    trail.write_text(
        json.dumps({"hook_event_name": "stop", "conversation_id": "plain", "model": "default"})
        + "\n",
        encoding="utf-8",
    )
    bundle = view.build_bundle(trail)
    assert bundle["verify"]["status"] == "n/a"
    assert bundle["verify"]["detail"] == "no signed TRACE yet"
    assert bundle["trace"] is None


def test_scan_local_trails_indexes_conversations(tmp_path: Path) -> None:
    a = tmp_path / "aaa.jsonl"
    a.write_text(
        json.dumps(
            {
                "conversation_id": "aaa",
                "workspace_roots": ["/ws/a"],
                "payload": {"user_email": "a@example.com"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "aaa.trace.json").write_text("{}", encoding="utf-8")
    b = tmp_path / "bbb.jsonl"
    b.write_text("{}\n{}\n", encoding="utf-8")

    entries = library.scan_local_trails([tmp_path])
    by_id = {e.id: e for e in entries}
    assert "aaa" in by_id and "bbb" in by_id
    assert by_id["aaa"].has_trace is True
    assert by_id["aaa"].user == "a@example.com"
    assert by_id["aaa"].workspace == "/ws/a"
    assert by_id["bbb"].event_count == 2
    assert by_id["bbb"].has_trace is False


def test_scan_local_respects_redacted_user(tmp_path: Path) -> None:
    trail = tmp_path / "red.jsonl"
    trail.write_text(
        json.dumps(
            {
                "payload": {
                    "user_email": {
                        "_redacted": True,
                        "present": True,
                        "type": "string",
                        "chars": 12,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entries = library.scan_local_dir(tmp_path)
    assert entries[0].user == "(redacted)"


def test_s3_config_soft_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_AGENT_TRACE_S3_BUCKET", raising=False)
    monkeypatch.delenv("CURSOR_ACTIVITY_S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    cfg = library.s3_config()
    assert cfg.enabled is False
    result = library.scan_s3_trails()
    assert result["ok"] is False
    assert result["configured"] is False
    assert "S3 not configured" in result["detail"]


def test_build_library_index_local_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(tmp_path))
    monkeypatch.delenv("CURSOR_AGENT_TRACE_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    (tmp_path / "c1.jsonl").write_text('{"conversation_id":"c1"}\n', encoding="utf-8")
    index = library.build_library_index(include_s3=False, extra_dirs=[tmp_path])
    assert index["counts"]["local"] >= 1
    assert any(t["id"] == "c1" for t in index["local"])


def test_resolve_trail_path_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(tmp_path))
    path = tmp_path / "conv-99.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    found = library.resolve_trail_path("conv-99", source="local", extra_dirs=[tmp_path])
    assert found == path.resolve()


def test_policy_deny_demo_fixture_bundle() -> None:
    """Checked-in fixture: mixed allow/deny + unsigned TRACE → verify FAIL."""
    from lib.demo import POLICY_DENY_FIXTURE

    assert POLICY_DENY_FIXTURE.is_file()
    bundle = view.build_bundle(POLICY_DENY_FIXTURE)
    decisions = [
        (e.get("policy_decision") or {}).get("decision")
        for e in bundle["events"]
        if isinstance(e.get("policy_decision"), dict)
    ]
    assert "allow" in decisions
    assert "deny" in decisions
    assert "would_deny" in decisions
    assert bundle["verify"]["status"] == "FAIL"
    assert bundle["verify"]["detail"]
    assert bundle["honesty"] is not None


def test_api_verify_returns_checks(tmp_path: Path) -> None:
    """``/api/verify`` re-runs soft_verify and returns a structured payload."""
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen

    trail = tmp_path / "api-verify.jsonl"
    trail.write_text(
        json.dumps(
            {
                "hook_event_name": "stop",
                "conversation_id": "api-verify",
                "model": "default",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "api-verify.trace.json").write_text(
        json.dumps(
            {
                "subject": "spiffe://local.cursor/agent/hooks/conversation/api-verify",
                "signature": "abc",
                "cnf": {"jwk": {"kty": "OKP"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bundle = view.build_bundle(trail)
    state = view._ViewerState(
        viewer_dir=view.VIEWER_DIR,
        mode="single",
        jsonl=trail.resolve(),
        bundle=bundle,
        extra_dirs=[tmp_path],
        include_s3=False,
    )
    handler = view._make_handler(state)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/verify?id=api-verify&source=local", timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["status"] in {"PASS", "FAIL", "signed"}
        assert "detail" in payload
        assert isinstance(payload.get("checks"), list)
        # Missing CLI → signed; with CLI fake sig → FAIL. Either way, not silent empty.
        assert payload["detail"]
    finally:
        httpd.shutdown()
        httpd.server_close()
