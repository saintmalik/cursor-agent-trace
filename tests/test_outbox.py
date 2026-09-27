"""Outbox queue: enqueue, flush, backoff, replay drain."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from lib.outbox import Outbox, PostResult


def _ok_post(payload: dict[str, Any]) -> PostResult:
    return PostResult(ok=True, status=200, response={"ok": True, "kind": payload.get("kind")})


def _fail_post(payload: dict[str, Any]) -> PostResult:
    return PostResult(ok=False, status=503, error="collector down", url="http://test/v1/ingest")


@pytest.fixture
def box(tmp_path: Path) -> Outbox:
    return Outbox(tmp_path / "agent-trace")


def test_enqueue_and_pending_count(box: Outbox):
    box.enqueue_event(conversation_id="c1", body={"hook_event_name": "stop"})
    box.enqueue_event(conversation_id="c1", body={"hook_event_name": "stop"})
    assert box.pending_count() == 2


def test_flush_success_drains_pending(box: Outbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_BATCH_SIZE", "2")
    monkeypatch.setenv("COLLECTOR_FLUSH_INTERVAL_SEC", "0")
    box.enqueue_event(conversation_id="c1", body={"n": 1})
    box.enqueue_event(conversation_id="c1", body={"n": 2})
    result = box.flush(_ok_post, force=True)
    assert result["flushed"] is True
    assert result["ok"] is True
    assert result["batch_size"] == 2
    assert box.pending_count() == 0
    assert box.outbox_count() == 0


def test_flush_failure_enqueues_outbox(box: Outbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_BATCH_SIZE", "1")
    monkeypatch.setenv("COLLECTOR_FLUSH_INTERVAL_SEC", "0")
    box.enqueue_event(conversation_id="c1", body={"n": 1})
    result = box.flush(_fail_post, force=True)
    assert result["flushed"] is True
    assert result["ok"] is False
    assert result["queued"]
    assert box.pending_count() == 0
    assert box.outbox_count() == 1
    entry = json.loads(Path(result["queued"]).read_text(encoding="utf-8"))
    assert entry["payload"]["kind"] == "batch"
    assert entry["attempts"] == 0
    assert entry["next_attempt_at"] > time.time() - 1


def test_post_or_queue_on_failure(box: Outbox):
    result = box.post_or_queue({"kind": "trace", "body": {"x": 1}}, _fail_post)
    assert result["ok"] is False
    assert box.outbox_count() == 1


def test_replay_respects_backoff(box: Outbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_RETRY_BASE_SEC", "60")
    path = box.write_outbox({"kind": "batch", "events": []}, error="down")
    entry = json.loads(path.read_text(encoding="utf-8"))
    # Still in the future → deferred
    assert entry["next_attempt_at"] > time.time()
    replay = box.replay_outbox(_ok_post)
    assert replay["deferred"] == 1
    assert replay["succeeded"] == 0
    assert box.outbox_count() == 1


def test_replay_drain_when_due(box: Outbox):
    path = box.write_outbox({"kind": "batch", "events": [{"n": 1}]}, error="down")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["next_attempt_at"] = time.time() - 1
    path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")

    replay = box.replay_outbox(_ok_post)
    assert replay["succeeded"] == 1
    assert replay["remaining"] == 0
    assert box.outbox_count() == 0


def test_replay_backoff_increases_on_failure(box: Outbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_RETRY_BASE_SEC", "2")
    monkeypatch.setenv("COLLECTOR_RETRY_MAX_SEC", "100")
    path = box.write_outbox({"kind": "batch", "events": []}, error="down")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["next_attempt_at"] = time.time() - 1
    path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")

    before = time.time()
    replay = box.replay_outbox(_fail_post)
    assert replay["failed"] == 1
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["attempts"] == 1
    assert updated["next_attempt_at"] >= before + 2 - 0.5


def test_should_flush_by_batch_size(box: Outbox, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLLECTOR_BATCH_SIZE", "3")
    monkeypatch.setenv("COLLECTOR_FLUSH_INTERVAL_SEC", "9999")
    box.enqueue_event(conversation_id="c1", body={"n": 1})
    box.enqueue_event(conversation_id="c1", body={"n": 2})
    assert box.should_flush() is False
    box.enqueue_event(conversation_id="c1", body={"n": 3})
    assert box.should_flush() is True


def test_should_flush_force(box: Outbox):
    assert box.should_flush(force=True) is False
    box.enqueue_event(conversation_id="c1", body={"n": 1})
    assert box.should_flush(force=True) is True
