"""Redaction: safe vs full (off) modes from the hook module."""

from __future__ import annotations

import pytest


def test_safe_redacts_sensitive_keys(hook_mod):
    payload = {
        "prompt": "hello secret TOKEN=abc",
        "user_email": "redacted@example.com",
        "model": "default",
        "tool_input": {"command": "echo hi"},
        "nested": {"text": "assistant body"},
    }
    out = hook_mod.redact_payload(payload, "safe")
    assert out["model"] == "default"
    assert isinstance(out["prompt"], dict)
    assert out["prompt"]["_redacted"] is True
    assert "TOKEN=abc" not in str(out["prompt"])
    assert out["prompt"]["chars"] == len("hello secret TOKEN=abc")
    assert out["user_email"]["_redacted"] is True
    assert out["tool_input"]["_redacted"] is True
    assert out["nested"]["text"]["_redacted"] is True


def test_full_off_keeps_bodies(hook_mod):
    payload = {
        "prompt": "config-file FULL body visible",
        "user_email": "cfg@example.com",
    }
    out = hook_mod.redact_payload(payload, "off")
    assert out["prompt"] == "config-file FULL body visible"
    assert out["user_email"] == "cfg@example.com"


def test_redact_mode_from_env(hook_mod, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CURSOR_AGENT_TRACE_REDACT", "safe")
    assert hook_mod.redact_mode() == "safe"
    monkeypatch.setenv("CURSOR_AGENT_TRACE_REDACT", "full")
    assert hook_mod.redact_mode() == "off"
    monkeypatch.setenv("CURSOR_AGENT_TRACE_REDACT", "off")
    assert hook_mod.redact_mode() == "off"


def test_summarize_value_string(hook_mod):
    summary = hook_mod.summarize_value("abc")
    assert summary["present"] is True
    assert summary["chars"] == 3
    assert len(summary["sha256"]) == 64
