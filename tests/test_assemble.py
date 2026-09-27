"""Assemble + sign software-observed TRACE records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import assemble
from lib.model_identity import resolve_model_identity


pytestmark = pytest.mark.integration


def test_build_level0_unsigned_fields(
    sample_events, enforce_policy: Path, monkeypatch: pytest.MonkeyPatch
):
    identity = resolve_model_identity(sample_events)
    record = assemble.build_level0_record(
        sample_events,
        model=identity,
        iat=1_700_000_000,
    )
    assert record["eat_profile"] == assemble.TRACE_PROFILE
    assert record["runtime"]["platform"] == "software-only"
    assert record["appraisal"]["status"] == "none"
    assert record["origin"]["kind"] == "third-party-control-plane"
    assert record["origin"]["producer"].startswith("cursor-agent-trace@")
    assert record["model"]["provider"] == "cursor-asserted"
    assert record["model"]["model_id"] == "composer-2"
    assert record["policy"]["enforcement_mode"] == "enforce"
    assert record["policy"]["bundle_hash"].startswith("sha256:")
    assert "tool_transcript" in record
    assert record["tool_transcript"]["call_count"] >= 1
    assert "signature" not in record


def test_sign_level0_adds_signature_and_cnf(sample_events, enforce_policy: Path):
    pytest.importorskip("agentrust_trace")
    identity = resolve_model_identity(sample_events)
    unsigned = assemble.build_level0_record(sample_events, model=identity, iat=1_700_000_000)
    signed = assemble.sign_level0_record(unsigned)
    assert "signature" in signed and signed["signature"]
    assert "cnf" in signed


def test_assemble_and_sign_from_trail_writes_artifacts(
    write_trail, enforce_policy: Path, trail_dir: Path
):
    pytest.importorskip("agentrust_trace")
    trail = write_trail()
    dest, signed, identity = assemble.assemble_and_sign_from_trail(trail)
    assert dest.is_file()
    assert dest.name.endswith(".trace.json")
    assert signed["model"]["model_id"] == "composer-2"
    honesty = dest.with_name(dest.name.replace(".trace.json", ".honesty.json"))
    assert honesty.is_file()
    side = json.loads(honesty.read_text(encoding="utf-8"))
    assert side["weights_attested"] is False
    assert side["tee"] is False
    assert side["attestation"] == "none"
    assert side["runtime_platform"] == "software-only"
    assert identity.from_operator_override is False


def test_empty_trail_raises():
    with pytest.raises(assemble.MissingEvidence, match="empty trail"):
        assemble.build_level0_record([])


def test_observe_policy_maps_to_advisory(
    sample_events, observe_policy: Path
):
    record = assemble.build_level0_record(sample_events, iat=1_700_000_000)
    assert record["policy"]["enforcement_mode"] == "advisory"


def test_default_model_honesty(write_trail, enforce_policy: Path):
    pytest.importorskip("agentrust_trace")
    events = [
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "c-default",
            "model": "default",
            "payload": {"prompt": "x"},
        },
        {
            "hook_event_name": "stop",
            "conversation_id": "c-default",
            "model": "default",
            "status": "completed",
        },
    ]
    trail = write_trail("c-default.jsonl", events)
    _, signed, identity = assemble.assemble_and_sign_from_trail(trail)
    assert signed["model"]["model_id"] == "default"
    assert identity.from_operator_override is False
