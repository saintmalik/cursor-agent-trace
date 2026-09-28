"""Tab classification + consolidated trail path selection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import library, tab_trail


def test_is_tab_scoped_by_hook_name():
    assert tab_trail.is_tab_scoped({"hook_event_name": "beforeTabFileRead"})
    assert tab_trail.is_tab_scoped({"hook_event_name": "afterTabFileEdit"})
    assert not tab_trail.is_tab_scoped({"hook_event_name": "beforeReadFile"})
    assert not tab_trail.is_tab_scoped({"hook_event_name": "preToolUse", "model": "default"})


def test_is_tab_scoped_by_model_tab():
    assert tab_trail.is_tab_scoped(
        {
            "hook_event_name": "beforeSubmitPrompt",
            "model": "tab",
            "conversation_id": "uuid-1",
        }
    )
    assert tab_trail.is_tab_scoped({"model": "TAB", "conversation_id": "x"})


def test_is_tab_scoped_by_composer_mode():
    assert tab_trail.is_tab_scoped({"composer_mode": "tab", "conversation_id": "x"})
    assert tab_trail.is_tab_scoped(
        {"payload": {"composer_mode": "tab", "hook_event_name": "beforeTabFileRead"}}
    )


def test_agent_events_not_tab_scoped():
    assert not tab_trail.is_tab_scoped(
        {
            "hook_event_name": "preToolUse",
            "model": "default",
            "conversation_id": "agent-uuid",
        }
    )
    assert not tab_trail.is_tab_scoped(
        {
            "hook_event_name": "subagentStart",
            "model": "cursor-grok-4.5-high",
            "conversation_id": "child",
            "parent_conversation_id": "parent",
        }
    )


def test_tab_trail_stem_stable_and_daily(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CURSOR_AGENT_TRACE_TAB_ROTATE", raising=False)
    assert tab_trail.tab_trail_stem() == "tab"
    monkeypatch.setenv("CURSOR_AGENT_TRACE_TAB_ROTATE", "daily")
    when = datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert tab_trail.tab_trail_stem(when=when) == "tab-20260928"


def test_is_tab_trail_stem():
    assert tab_trail.is_tab_trail_stem("tab")
    assert tab_trail.is_tab_trail_stem("tab-20260928")
    assert not tab_trail.is_tab_trail_stem("tab-extra")
    assert not tab_trail.is_tab_trail_stem("02104edc-3956-4914-afd6-6df5040331d0")


def test_conversation_key_tab_uses_stem():
    payload = {
        "hook_event_name": "beforeTabFileRead",
        "model": "tab",
        "conversation_id": "feeeceae-1f5a-4a22-8064-d7d56a0c6bc8",
    }
    assert tab_trail.conversation_key_for_trail(payload) == "tab"


def test_conversation_key_agent_unchanged():
    payload = {
        "hook_event_name": "preToolUse",
        "model": "default",
        "conversation_id": "agent-uuid-99",
    }
    assert tab_trail.conversation_key_for_trail(payload) == "agent-uuid-99"


def test_hook_trail_path_routes_tab_to_tab_jsonl(
    hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))
    path = hook_mod.trail_path_for(
        {
            "hook_event_name": "beforeTabFileRead",
            "model": "tab",
            "conversation_id": "uuid-aaa",
        }
    )
    assert path == trail_dir / "tab.jsonl"

    agent = hook_mod.trail_path_for(
        {
            "hook_event_name": "preToolUse",
            "model": "default",
            "conversation_id": "agent-bbb",
        }
    )
    assert agent == trail_dir / "agent-bbb.jsonl"


def test_hook_append_two_tab_reads_same_file(
    hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))
    monkeypatch.setenv("CURSOR_AGENT_TRACE_SIGN", "0")

    for cid in ("uuid-1", "uuid-2"):
        raw = {
            "hook_event_name": "beforeTabFileRead",
            "conversation_id": cid,
            "generation_id": cid,
            "model": "tab",
            "file_path": f"/tmp/{cid}.py",
            "workspace_roots": ["/tmp"],
        }
        path = hook_mod.trail_path_for(raw)
        record = hook_mod.build_record(raw)
        hook_mod.append_jsonl(path, record)

    trail = trail_dir / "tab.jsonl"
    assert trail.is_file()
    assert not (trail_dir / "uuid-1.jsonl").exists()
    assert not (trail_dir / "uuid-2.jsonl").exists()
    lines = [json.loads(l) for l in trail.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert {row["conversation_id"] for row in lines} == {"uuid-1", "uuid-2"}
    assert all(row["model"] == "tab" for row in lines)


def test_maybe_sign_skips_tab_trail(
    hook_mod, trail_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_DIR", str(trail_dir))
    monkeypatch.setenv("CURSOR_AGENT_TRACE_SIGN", "1")
    trail = trail_dir / "tab.jsonl"
    trail.write_text(
        json.dumps(
            {
                "hook_event_name": "beforeTabFileRead",
                "conversation_id": "u1",
                "model": "tab",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = hook_mod.maybe_sign_trace(
        {"hook_event_name": "stop", "conversation_id": "u1", "model": "tab"},
        trail,
    )
    assert out is None
    assert not (trail_dir / "tab.trace.json").exists()


def test_library_lists_consolidated_tab_trail(tmp_path: Path):
    trail = tmp_path / "tab.jsonl"
    trail.write_text(
        json.dumps(
            {
                "hook_event_name": "beforeTabFileRead",
                "conversation_id": "u1",
                "model": "tab",
                "workspace_roots": ["/proj"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "agent-1.jsonl").write_text(
        json.dumps({"conversation_id": "agent-1", "model": "default"}) + "\n",
        encoding="utf-8",
    )
    entries = library.scan_local_dir(tmp_path)
    by_id = {e.id: e for e in entries}
    assert "tab" in by_id
    assert by_id["tab"].kind == "tab"
    assert by_id["tab"].model_id == "tab"
    assert by_id["tab"].event_count == 1
    assert by_id["agent-1"].kind == "agent"


def test_library_labels_subagent_and_parent(tmp_path: Path):
    parent = tmp_path / "parent-conv.jsonl"
    parent.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "hook_event_name": "subagentStart",
                        "conversation_id": "parent-conv",
                        "model": "default",
                        "payload": {
                            "parent_conversation_id": "parent-conv",
                            "subagent_id": "child-tool",
                        },
                    }
                ),
                json.dumps(
                    {
                        "hook_event_name": "stop",
                        "conversation_id": "parent-conv",
                        "model": "default",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    child = tmp_path / "child-conv.jsonl"
    child.write_text(
        json.dumps(
            {
                "hook_event_name": "beforeSubmitPrompt",
                "conversation_id": "child-conv",
                "model": "default",
                "payload": {"parent_conversation_id": "parent-conv"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "child-conv.honesty.json").write_text(
        json.dumps(
            {
                "inherited_from_parent": True,
                "parent_conversation_id": "parent-conv",
                "model_id_in_record": "grok-4.5",
            }
        ),
        encoding="utf-8",
    )
    entries = library.scan_local_dir(tmp_path)
    by_id = {e.id: e for e in entries}
    assert by_id["child-conv"].kind == "subagent"
    assert by_id["child-conv"].parent_id == "parent-conv"
    assert by_id["parent-conv"].kind == "parent"

