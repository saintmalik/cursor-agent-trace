"""Unit tests for lib.model_identity."""

from __future__ import annotations

import json

import pytest

from lib.model_identity import resolve_model_identity


def test_default_generic_slug_recorded_honestly():
    identity = resolve_model_identity(
        [{"hook_event_name": "stop", "model": "default", "conversation_id": "c1"}]
    )
    assert identity.provider == "cursor-asserted"
    assert identity.model_id == "default"
    assert identity.from_operator_override is False
    assert any("generic" in n.lower() for n in identity.notes)
    assert not any("CURSOR_" in n for n in identity.notes)


def test_missing_model_recorded_as_unknown():
    identity = resolve_model_identity([{"hook_event_name": "stop", "conversation_id": "c1"}])
    assert identity.provider == "cursor-asserted"
    assert identity.model_id == "unknown"
    assert identity.from_operator_override is False
    assert any("unknown" in n.lower() for n in identity.notes)


def test_prefer_non_generic_model_id_over_default_slug():
    identity = resolve_model_identity(
        [
            {
                "hook_event_name": "beforeSubmitPrompt",
                "model": "default",
                "model_id": "grok-4.5",
            }
        ]
    )
    assert identity.model_id == "grok-4.5"
    assert identity.provider == "cursor-asserted"
    assert identity.from_operator_override is False
    assert identity.asserted_model_ids == ("grok-4.5",)


def test_concrete_hook_model_slug_used_when_no_model_id():
    identity = resolve_model_identity(
        [
            {
                "hook_event_name": "preToolUse",
                "model": "cursor-grok-4.5-high",
            }
        ]
    )
    assert identity.model_id == "cursor-grok-4.5-high"
    assert identity.from_operator_override is False


def test_operator_override_fills_generic_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_MODEL_ID", "sdk-lab-override")
    identity = resolve_model_identity(
        [{"hook_event_name": "stop", "model": "default"}]
    )
    assert identity.model_id == "sdk-lab-override"
    assert identity.provider == "operator-declared"
    assert identity.from_operator_override is True
    assert any("declared" in n.lower() for n in identity.notes)


def test_legacy_env_alias_still_works(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CURSOR_AGENT_TRACE_MODEL_ID", raising=False)
    monkeypatch.setenv("CURSOR_TRACE_MODEL_ID", "legacy-alias")
    identity = resolve_model_identity([{"model": "default"}])
    assert identity.model_id == "legacy-alias"
    assert identity.from_operator_override is True


def test_operator_override_ignored_when_hook_is_specific(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_MODEL_ID", "sdk-lab-override")
    identity = resolve_model_identity(
        [{"hook_event_name": "stop", "model": "default", "model_id": "grok-4.5"}]
    )
    assert identity.model_id == "grok-4.5"
    assert identity.provider == "cursor-asserted"
    assert identity.from_operator_override is False
    assert any("ignored declared override" in n.lower() for n in identity.notes)


def test_explicit_override_kwarg_beats_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_MODEL_ID", "from-env")
    identity = resolve_model_identity(
        [{"model": "default"}],
        override="from-kwarg",
    )
    assert identity.model_id == "from-kwarg"
    assert identity.provider == "operator-declared"
    assert identity.from_operator_override is True


def test_nested_payload_model_id_preferred():
    identity = resolve_model_identity(
        [
            {
                "model": "default",
                "payload": {
                    "composer": {"modelId": "nested-composer-pro"},
                },
            }
        ]
    )
    assert identity.model_id == "nested-composer-pro"


def test_last_non_generic_wins_across_events():
    identity = resolve_model_identity(
        [
            {"model_id": "first-model"},
            {"model_id": "second-model"},
            {"model": "default"},
        ]
    )
    assert identity.model_id == "second-model"


def test_parent_inheritance_via_tool_call_id(tmp_path, monkeypatch):
    monkeypatch.delenv("CURSOR_AGENT_TRACE_MODEL_ID", raising=False)
    monkeypatch.delenv("CURSOR_TRACE_MODEL_ID", raising=False)
    parent = tmp_path / "parent-conv.jsonl"
    parent.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "hook_event_name": "afterAgentThought",
                        "conversation_id": "parent-conv",
                        "model": "cursor-grok-4.5-high",
                        "model_id": "grok-4.5",
                    }
                ),
                json.dumps(
                    {
                        "hook_event_name": "subagentStart",
                        "conversation_id": "parent-conv",
                        "payload": {
                            "subagent_id": "call-abc\nfc_xyz",
                            "parent_conversation_id": "parent-conv",
                            "subagent_model": "default",
                            "model": "default",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "parent-conv.honesty.json").write_text(
        json.dumps({"model_id_in_record": "grok-4.5", "asserted_model_ids": ["grok-4.5"]})
        + "\n",
        encoding="utf-8",
    )
    child = tmp_path / "child-conv.jsonl"
    child.write_text(
        json.dumps(
            {
                "hook_event_name": "preToolUse",
                "conversation_id": "child-conv",
                "model": "default",
                "payload": {
                    "parent_tool_call_id": "call-abc\nfc_xyz",
                    "model": "default",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    from lib.model_identity import resolve_model_identity as resolve

    events = [json.loads(line) for line in child.read_text().splitlines() if line.strip()]
    identity = resolve(events, trail_path=child)
    assert identity.inherited_from_parent is True
    assert identity.provider == "inherited-from-parent-conversation"
    assert identity.model_id == "grok-4.5"
    assert identity.parent_conversation_id == "parent-conv"


def test_asserted_histogram_counts_distinct_hits():
    identity = resolve_model_identity(
        [
            {"model": "default", "model_id": "grok-4.5"},
            {"model": "default"},
            {"payload": {"subagent_model": "default"}},
        ]
    )
    hist = dict(identity.asserted_histogram)
    assert hist.get("default", 0) >= 2
    assert hist.get("grok-4.5", 0) >= 1


def test_local_state_lookup_skipped_note():
    identity = resolve_model_identity([{"model": "default"}])
    assert "skipped" in identity.local_state_lookup.lower()
    assert any("local state" in n.lower() for n in identity.notes)
