"""Hook batch-buffer flush logic (enqueue → size/interval flush → outbox)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lib.outbox import Outbox, PostResult


def test_hook_enqueue_and_flush_collector_success(hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_URL", "http://127.0.0.1:9")  # unused — we inject poster
    monkeypatch.setenv("COLLECTOR_TOKEN", "tok")
    monkeypatch.setenv("COLLECTOR_BATCH_SIZE", "2")
    monkeypatch.setenv("COLLECTOR_FLUSH_INTERVAL_SEC", "0")
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))

    posts: list[dict[str, Any]] = []

    def fake_poster(payload: dict[str, Any]) -> PostResult:
        posts.append(payload)
        return PostResult(ok=True, status=200, response={"ok": True, "kind": payload.get("kind")})

    monkeypatch.setattr(hook_mod, "collector_poster", lambda: fake_poster)

    trail = trail_dir / "flush-conv.jsonl"
    trail.write_text("", encoding="utf-8")
    record = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "flush-conv", "n": 1}

    hook_mod.enqueue_and_flush_collector(
        trail=trail,
        conversation_id="flush-conv",
        record=record,
        force_flush=False,
    )
    # One event — batch size 2 → may not flush yet depending on interval=0
    box = Outbox(trail_dir)
    # interval 0 → should_flush True once any pending
    assert box.pending_count() + len(posts) >= 1

    hook_mod.enqueue_and_flush_collector(
        trail=trail,
        conversation_id="flush-conv",
        record={**record, "n": 2},
        force_flush=False,
    )
    # After two events with batch_size=2, at least one batch POST expected.
    assert any(p.get("kind") == "batch" for p in posts)
    assert box.pending_count() == 0


def test_hook_flush_failure_lands_in_outbox(hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("COLLECTOR_TOKEN", "tok")
    monkeypatch.setenv("COLLECTOR_BATCH_SIZE", "1")
    monkeypatch.setenv("COLLECTOR_FLUSH_INTERVAL_SEC", "0")
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))

    def fail_poster(payload: dict[str, Any]) -> PostResult:
        return PostResult(ok=False, error="down", status=503)

    monkeypatch.setattr(hook_mod, "collector_poster", lambda: fail_poster)

    trail = trail_dir / "fail-conv.jsonl"
    trail.write_text("", encoding="utf-8")
    hook_mod.enqueue_and_flush_collector(
        trail=trail,
        conversation_id="fail-conv",
        record={"hook_event_name": "stop", "conversation_id": "fail-conv"},
        force_flush=True,
    )
    box = Outbox(trail_dir)
    assert box.outbox_count() >= 1
    meta = trail.with_suffix(".collector.jsonl")
    assert meta.is_file()
    rows = [json.loads(l) for l in meta.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows


def test_hook_force_flush_on_stop(hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("COLLECTOR_TOKEN", "tok")
    monkeypatch.setenv("COLLECTOR_BATCH_SIZE", "100")
    monkeypatch.setenv("COLLECTOR_FLUSH_INTERVAL_SEC", "9999")
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))

    posts: list[dict[str, Any]] = []

    def ok_poster(payload: dict[str, Any]) -> PostResult:
        posts.append(payload)
        return PostResult(ok=True, status=200, response={"ok": True})

    monkeypatch.setattr(hook_mod, "collector_poster", lambda: ok_poster)

    trail = trail_dir / "stop-flush.jsonl"
    trail.write_text("", encoding="utf-8")
    hook_mod.enqueue_and_flush_collector(
        trail=trail,
        conversation_id="stop-flush",
        record={"hook_event_name": "beforeSubmitPrompt", "n": 1},
        force_flush=False,
    )
    assert not posts  # batch size / interval not met
    hook_mod.enqueue_and_flush_collector(
        trail=trail,
        conversation_id="stop-flush",
        record={"hook_event_name": "stop", "n": 2},
        force_flush=True,
    )
    assert any(p.get("kind") == "batch" for p in posts)
    assert Outbox(trail_dir).pending_count() == 0


def test_missing_token_logs_meta_no_crash(hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("COLLECTOR_TOKEN", raising=False)
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))
    trail = trail_dir / "no-tok.jsonl"
    trail.write_text("", encoding="utf-8")
    hook_mod.enqueue_and_flush_collector(
        trail=trail,
        conversation_id="no-tok",
        record={"hook_event_name": "stop"},
        force_flush=True,
    )
    meta = trail.with_suffix(".collector.jsonl")
    assert meta.is_file()
    row = json.loads(meta.read_text(encoding="utf-8").splitlines()[0])
    assert "COLLECTOR_TOKEN" in row.get("error", "")
