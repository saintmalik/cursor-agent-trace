"""Cursor Agent TRACE — assemble + sign software-observed records from official Cursor hook trails."""

from .assemble import VERSION, build_level0_record, load_jsonl_events, sign_level0_record
from .model_identity import ModelIdentity, env_model_override, resolve_model_identity

__all__ = [
    "VERSION",
    "ModelIdentity",
    "build_level0_record",
    "env_model_override",
    "load_jsonl_events",
    "resolve_model_identity",
    "sign_level0_record",
]
