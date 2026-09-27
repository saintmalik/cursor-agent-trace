"""Resolve Cursor-asserted model identity without fabricating provider/weights.

Primary path: whatever Cursor puts on the hook event (``model``, ``model_id``,
and nested equivalents). Those values are recorded as-is — including generic
slugs like ``default`` when that is all Cursor sent.

Do **not** babysit an env var when switching models in the UI. Model identity
follows the trail; mid-conversation switches pick the latest non-generic
assertion.

When a subagent trail only has generic slugs but a sibling parent trail has a
concrete ``cursor-…`` / ``grok-…`` (etc.) assertion, we may surface that as
``inherited-from-parent-conversation`` — labeled secondary evidence, never
pretended to be observed on this trail's hooks.

Lab escape hatch only (declared, never treated as observed):
  ``CURSOR_AGENT_TRACE_MODEL_ID`` (alias: ``CURSOR_TRACE_MODEL_ID``)

When set, the override fills a generic/missing hook value and is labeled
``operator-declared``. Prefer leaving it unset.

Cursor local DB scrapers are intentionally **not** shipped.
``cursorDiskKV`` key ``composerData:<conversation_id>`` can expose
``modelConfig.modelName`` / ``selectedModels[].modelId``, but when hooks only
assert ``default`` (Auto), that store usually asserts ``default`` too; bubble
``modelInfo`` is sparse and can disagree with hook slugs. Scraping would not
recover a reliable concrete slug for TRACE labeling.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Values that mean "Cursor did not name a concrete model slug".
_GENERIC_SLUGS = frozenset({"", "default", "unknown", "none", "null"})

# Canonical lab override; short alias kept for older test/env files.
_ENV_MODEL_KEYS = ("CURSOR_AGENT_TRACE_MODEL_ID", "CURSOR_TRACE_MODEL_ID")

# Nested keys that look like model *slugs* (product names).
_SLUG_KEYS = frozenset(
    {
        "model",
        "model_slug",
        "modelSlug",
        "modelName",
        "model_name",
        "subagent_model",
        "subagentModel",
        "composer",
        "composer_model",
        "composerModel",
    }
)

# Nested keys that look like model *ids*.
_ID_KEYS = frozenset(
    {
        "model_id",
        "modelId",
        "composer_model_id",
        "composerModelId",
        "composer_modelId",
    }
)

_VERSION_KEYS = frozenset({"model_version", "modelVersion"})

# Parent linkage keys Cursor has been observed to put on hook payloads.
_PARENT_CONV_KEYS = frozenset(
    {
        "parent_conversation_id",
        "parentConversationId",
        "parent_session_id",
        "parentSessionId",
    }
)
_PARENT_TOOL_KEYS = frozenset(
    {
        "parent_tool_call_id",
        "parentToolCallId",
        "subagent_id",
        "subagentId",
    }
)

# Cap sibling JSONL scans when resolving parent by tool-call id.
_PARENT_SCAN_MAX_FILES = 80
_PARENT_SCAN_MAX_BYTES = 8_000_000


@dataclass(frozen=True)
class ModelIdentity:
    """Honest model block inputs for a software-observed TRACE record."""

    provider: str
    model_id: str
    version: str | None
    #: Human-readable notes for local sidecar / honesty docs (not TRACE fields).
    notes: tuple[str, ...]
    #: Raw distinct asserted strings seen across the trail (for debugging).
    asserted_slugs: tuple[str, ...]
    asserted_model_ids: tuple[str, ...]
    #: True when model_id came from a lab env/kwarg override (declared, not observed).
    from_operator_override: bool = False
    #: True when model_id came from a sibling parent conversation trail.
    inherited_from_parent: bool = False
    #: Parent conversation id when inheritance applied (or was attempted).
    parent_conversation_id: str | None = None
    #: Count of every model-like string seen (slug + id), for viewer histogram.
    asserted_histogram: tuple[tuple[str, int], ...] = ()
    #: Local Cursor state probe result (skipped: store also says default for Auto).
    local_state_lookup: str = (
        "skipped: composerData.modelConfig mirrors hook default under Auto; "
        "no reliable concrete slug"
    )


def _as_nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text and text.lower() not in {"null", "none"}:
            return text
    return None


def env_model_override() -> str | None:
    """Lab-only declared model id. Prefer unset — hooks are the source of truth."""
    for key in _ENV_MODEL_KEYS:
        text = _as_nonempty_str(os.environ.get(key))
        if text is not None:
            return text
    return None


def _walk_for_keys(obj: Any, keys: frozenset[str], out: list[str], *, depth: int = 0) -> None:
    """Collect string values for named keys anywhere in a nested payload."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys:
                text = _as_nonempty_str(value)
                if text is not None:
                    out.append(text)
                elif isinstance(value, (dict, list)):
                    _walk_for_keys(value, keys, out, depth=depth + 1)
            else:
                _walk_for_keys(value, keys, out, depth=depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_keys(item, keys, out, depth=depth + 1)


def _prefer_specific(candidates: list[str]) -> str | None:
    """Prefer a non-generic slug when Cursor also asserted one."""
    specific = [c for c in candidates if c.strip().lower() not in _GENERIC_SLUGS]
    if specific:
        # Stable: last non-generic wins (most recent hook event usually last in trail).
        return specific[-1]
    if candidates:
        return candidates[-1]
    return None


def _is_generic(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() in _GENERIC_SLUGS


def _uniq(seq: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _histogram(slugs: list[str], model_ids: list[str]) -> tuple[tuple[str, int], ...]:
    counts: Counter[str] = Counter()
    for item in slugs:
        counts[item] += 1
    for item in model_ids:
        counts[item] += 1
    return tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def collect_model_assertions(
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Scan every event (full nested tree) for model-like fields."""
    slugs: list[str] = []
    model_ids: list[str] = []
    versions: list[str] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        # Full-tree walk covers top-level, payload, and deeper nests once each.
        _walk_for_keys(event, _SLUG_KEYS, slugs)
        _walk_for_keys(event, _ID_KEYS, model_ids)
        nested_versions: list[str] = []
        _walk_for_keys(event, _VERSION_KEYS, nested_versions)
        for ver in nested_versions:
            lowered = ver.lower()
            if lowered in _GENERIC_SLUGS or lowered in {"string", "number", "object"}:
                continue
            if ver.isdigit() and int(ver) < 10:
                continue
            versions.append(ver)

    return slugs, model_ids, versions

def extract_parent_linkage(
    events: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Return ``(parent_conversation_id, parent_tool_call_id)`` when present.

    Prefers an explicit parent conversation id. ``parent_tool_call_id`` (or a
    matching ``subagent_id`` on the parent trail) is a secondary locator.
    """
    parent_convs: list[str] = []
    parent_tools: list[str] = []
    self_ids: set[str] = set()

    for event in events:
        if not isinstance(event, dict):
            continue
        for key in ("conversation_id", "session_id"):
            text = _as_nonempty_str(event.get(key))
            if text is not None:
                self_ids.add(text)
        payload = event.get("payload")
        if isinstance(payload, dict):
            for key in ("conversation_id", "session_id"):
                text = _as_nonempty_str(payload.get(key))
                if text is not None:
                    self_ids.add(text)

        found_conv: list[str] = []
        found_tool: list[str] = []
        _walk_for_keys(event, _PARENT_CONV_KEYS, found_conv)
        # Only treat parent_tool_call_id as a parent locator on *this* trail;
        # subagent_id on the child trail is usually the child's own id.
        _walk_for_keys(
            event,
            frozenset({"parent_tool_call_id", "parentToolCallId"}),
            found_tool,
        )
        parent_convs.extend(found_conv)
        parent_tools.extend(found_tool)

    parent_conv = None
    for cand in reversed(parent_convs):
        if cand not in self_ids:
            parent_conv = cand
            break
    # If parent_conversation_id equals this trail's id (subagentStart on the
    # parent), that is not a different parent — leave None.
    parent_tool = parent_tools[-1] if parent_tools else None
    return parent_conv, parent_tool


def _safe_stem(conversation_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in conversation_id)[:180]


def _load_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    return events


def _parent_model_from_artifacts(parent_stem: str, trail_dir: Path) -> tuple[str | None, str | None]:
    """Prefer signed TRACE / honesty on the parent, then parent JSONL."""
    honesty = trail_dir / f"{parent_stem}.honesty.json"
    if honesty.is_file():
        try:
            data = json.loads(honesty.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            mid = _as_nonempty_str(data.get("model_id_in_record"))
            if mid and not _is_generic(mid):
                return mid, parent_stem
            for mid in data.get("asserted_model_ids") or []:
                text = _as_nonempty_str(mid)
                if text and not _is_generic(text):
                    return text, parent_stem
            for slug in data.get("asserted_slugs") or []:
                text = _as_nonempty_str(slug)
                if text and not _is_generic(text):
                    return text, parent_stem

    trace = trail_dir / f"{parent_stem}.trace.json"
    if trace.is_file():
        try:
            data = json.loads(trace.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            model = data.get("model")
            if isinstance(model, dict):
                mid = _as_nonempty_str(model.get("model_id"))
                if mid and not _is_generic(mid):
                    return mid, parent_stem

    jsonl = trail_dir / f"{parent_stem}.jsonl"
    if jsonl.is_file():
        parent_events = _load_jsonl_dicts(jsonl)
        # One-level only: do not recurse into the parent's parent.
        identity = resolve_model_identity(parent_events, allow_parent_inheritance=False)
        if not _is_generic(identity.model_id) and not identity.from_operator_override:
            return identity.model_id, parent_stem
    return None, parent_stem


def find_parent_conversation(
    events: list[dict[str, Any]],
    *,
    trail_path: Path | None = None,
    trail_dir: Path | None = None,
) -> tuple[str | None, str | None]:
    """Locate a sibling parent conversation id and a concrete parent model_id.

    Returns ``(parent_conversation_id, parent_model_id)``. Either may be None.
    """
    parent_conv, parent_tool = extract_parent_linkage(events)
    directory = trail_dir
    self_stem: str | None = None
    if trail_path is not None:
        path = Path(trail_path)
        directory = directory or path.parent
        self_stem = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else path.stem

    if directory is None:
        return parent_conv, None

    directory = Path(directory)
    if not directory.is_dir():
        return parent_conv, None

    if parent_conv:
        stem = _safe_stem(parent_conv)
        if self_stem and stem == self_stem:
            return None, None
        mid, _ = _parent_model_from_artifacts(stem, directory)
        return parent_conv, mid

    if not parent_tool:
        return None, None

    # Resolve parent conversation by matching parent_tool_call_id → subagent_id
    # on a sibling trail's subagentStart event.
    # JSONL on disk escapes newlines (\\n); compare against parsed events, not raw text.
    scanned = 0
    for candidate in sorted(directory.glob("*.jsonl")):
        if self_stem and candidate.stem == self_stem:
            continue
        scanned += 1
        if scanned > _PARENT_SCAN_MAX_FILES:
            break
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        if size > _PARENT_SCAN_MAX_BYTES:
            continue
        try:
            events_on_disk = _load_jsonl_dicts(candidate)
        except OSError:
            continue
        for event in events_on_disk:
            if event.get("hook_event_name") != "subagentStart":
                continue
            found: list[str] = []
            _walk_for_keys(event, frozenset({"subagent_id", "subagentId"}), found)
            if parent_tool not in found:
                continue
            # Parent conversation is this sibling's conversation_id.
            conv = _as_nonempty_str(event.get("conversation_id"))
            payload = event.get("payload")
            if conv is None and isinstance(payload, dict):
                conv = _as_nonempty_str(payload.get("conversation_id"))
            parent_ids: list[str] = []
            _walk_for_keys(event, _PARENT_CONV_KEYS, parent_ids)
            chosen_parent = parent_ids[-1] if parent_ids else conv
            if not chosen_parent or (self_stem and _safe_stem(chosen_parent) == self_stem):
                continue
            mid, _ = _parent_model_from_artifacts(_safe_stem(chosen_parent), directory)
            return chosen_parent, mid

    return None, None


def try_cursor_local_model(_conversation_id: str | None) -> str | None:
    """Optional Cursor local-state lookup — intentionally a no-op.

    A private key path exists (``cursorDiskKV`` / ``composerData:<id>`` →
    ``modelConfig``), but under Auto it stores the same generic ``default``
    hooks already send. Shipping a scraper would not fix TRACE labeling and
    would couple us to an undocumented schema. See lane probe notes.
    """
    return None


def resolve_model_identity(
    events: list[dict[str, Any]],
    *,
    override: str | None = None,
    trail_path: Path | None = None,
    trail_dir: Path | None = None,
    allow_parent_inheritance: bool = True,
) -> ModelIdentity:
    """Derive ``model.provider`` / ``model.model_id`` from accumulated hook events.

    Rules:
    - Prefer non-generic ``model_id`` / ``model`` from Cursor hook payloads.
    - If hooks only asserted a generic slug (or nothing), record that honestly
      (``default`` / ``unknown``) — never invent a concrete product slug.
    - Optional parent inheritance: when this trail is generic and a sibling
      parent conversation has a concrete assertion, use it with provider
      ``inherited-from-parent-conversation``.
    - Optional ``override`` / ``CURSOR_AGENT_TRACE_MODEL_ID`` is a lab escape
      hatch only: fills generic/missing values and sets provider to
      ``operator-declared`` (not observed).
    - ``version`` is set only when a distinct version-like field appears.
    - Never invent weights digests or TEE-bound model identity.
    """
    slugs, model_ids, versions = collect_model_assertions(events)
    hist = _histogram(slugs, model_ids)
    notes: list[str] = []
    from_override = False
    inherited = False
    parent_conversation_id: str | None = None
    local_note = (
        "skipped: composerData.modelConfig also 'default' under Auto "
        "(refusing brittle state.vscdb scrape)"
    )

    hook_id = _prefer_specific(model_ids) or _prefer_specific(slugs)
    chosen_slug = _prefer_specific(slugs)
    if override is not None:
        op_override = _as_nonempty_str(override)
    else:
        op_override = env_model_override()

    if not _is_generic(hook_id):
        chosen_id = hook_id  # type: ignore[assignment]
        provider = "cursor-asserted"
        if op_override and op_override != chosen_id:
            notes.append(
                f"Hook asserted non-generic model_id={chosen_id!r}; "
                f"ignored declared override {op_override!r}."
            )
        elif chosen_slug and _is_generic(chosen_slug):
            notes.append(
                f"Hook 'model' was generic ({chosen_slug!r}); using asserted model_id={chosen_id!r}."
            )
    elif op_override:
        chosen_id = op_override
        from_override = True
        provider = "operator-declared"
        notes.append(
            f"Hooks asserted only a generic/missing model; using declared lab override "
            f"model_id={op_override!r} via CURSOR_AGENT_TRACE_MODEL_ID "
            "(operator-declared — not observed from Cursor hooks)."
        )
    else:
        chosen_id = hook_id if hook_id is not None else "unknown"
        provider = "cursor-asserted"

        if allow_parent_inheritance:
            parent_id, parent_model = find_parent_conversation(
                events,
                trail_path=trail_path,
                trail_dir=trail_dir,
            )
            parent_conversation_id = parent_id
            if parent_model and not _is_generic(parent_model):
                chosen_id = parent_model
                provider = "inherited-from-parent-conversation"
                inherited = True
                notes.append(
                    f"This trail asserted only generic/missing model slugs; "
                    f"using parent conversation {parent_id!r} model_id={parent_model!r} "
                    f"(inherited-from-parent-conversation — secondary evidence, "
                    f"not observed on this trail's hooks)."
                )
            elif parent_id:
                notes.append(
                    f"Parent conversation {parent_id!r} found but had no concrete "
                    f"non-generic model to inherit."
                )

        if not inherited:
            if chosen_id == "unknown":
                notes.append(
                    "No model / model_id in hook payloads; recorded model_id='unknown'."
                )
            else:
                notes.append(
                    "Cursor asserted only a generic model slug (e.g. 'default'); "
                    "recorded as asserted — not invented from env or assumed weights."
                )

        # Local state probe (always skipped; honesty note only).
        conv = None
        for event in events:
            conv = _as_nonempty_str(event.get("conversation_id"))
            if conv:
                break
        if try_cursor_local_model(conv) is not None:  # pragma: no cover — always None
            pass
        else:
            notes.append(f"Cursor local state lookup {local_note}.")

    version = _prefer_specific(versions)

    return ModelIdentity(
        provider=provider,
        model_id=chosen_id,
        version=version,
        notes=tuple(notes),
        asserted_slugs=_uniq(slugs),
        asserted_model_ids=_uniq(model_ids),
        from_operator_override=from_override,
        inherited_from_parent=inherited,
        parent_conversation_id=parent_conversation_id,
        asserted_histogram=hist,
        local_state_lookup=local_note,
    )
