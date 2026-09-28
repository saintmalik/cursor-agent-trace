"""Tab event classification and consolidated trail path selection.

Cursor Tab (inline completion) fires ``beforeTabFileRead`` / ``afterTabFileEdit``
with ``model == "tab"`` and a fresh UUID per read. Without consolidation that
creates one tiny ``<uuid>.jsonl`` per Tab touch. Agent / Task / subagent trails
keep per-conversation UUID files (Cursor limitation).

Tab events always append to a stable ``tab.jsonl`` (or ``tab-YYYYMMDD.jsonl``
when daily rotate is enabled). Nothing is discarded — full event bodies still
land in JSONL. Tab stays JSONL-only (no invented Level-0 TRACE subject for a
bag of reads).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

TAB_HOOK_EVENTS = frozenset(
    {
        "beforeTabFileRead",
        "afterTabFileEdit",
    }
)

# Stable consolidated stem, or daily: tab-YYYYMMDD
_TAB_STEM_RE = re.compile(r"^tab(?:-\d{8})?$")


def tab_rotate_daily() -> bool:
    raw = (os.environ.get("CURSOR_AGENT_TRACE_TAB_ROTATE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "daily", "day"}


def tab_trail_stem(*, when: datetime | None = None) -> str:
    """Filename stem for the consolidated Tab trail (no ``.jsonl``)."""
    if tab_rotate_daily():
        ts = when or datetime.now(timezone.utc)
        return f"tab-{ts.strftime('%Y%m%d')}"
    return "tab"


def is_tab_trail_stem(stem: str) -> bool:
    """True for ``tab`` / ``tab-YYYYMMDD`` consolidated trail names."""
    return bool(_TAB_STEM_RE.match((stem or "").strip()))


def is_tab_scoped(payload: dict[str, Any]) -> bool:
    """Detect Tab-scoped hook payloads (keep Agent UUID trails unchanged)."""
    event = str(payload.get("hook_event_name") or "").strip()
    if event in TAB_HOOK_EVENTS:
        return True

    model = payload.get("model")
    if isinstance(model, str) and model.strip().lower() == "tab":
        return True

    for key in ("composer_mode", "mode"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().lower() == "tab":
            return True

    nested = payload.get("payload")
    if isinstance(nested, dict):
        nested_model = nested.get("model")
        if isinstance(nested_model, str) and nested_model.strip().lower() == "tab":
            return True
        nested_event = str(nested.get("hook_event_name") or "").strip()
        if nested_event in TAB_HOOK_EVENTS:
            return True
        for key in ("composer_mode", "mode"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip().lower() == "tab":
                return True

    return False


def conversation_key_for_trail(payload: dict[str, Any]) -> str:
    """Trail / collector grouping key — Tab uses the tab stem."""
    if is_tab_scoped(payload):
        return tab_trail_stem()
    for key in ("conversation_id", "session_id", "parent_conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"
