"""Snapshot pseudonymisation & pilot minimisation (design doc 04 §5).

Leadership chose pseudonymisation **on by default** for the non-EU/US LLM provider,
so no real character name reaches the model. This is a PURE, unit-testable transform
over the snapshot ``slices`` dict:

* any pilot reference (a node carrying a ``character_id``) has its name replaced
  with a stable opaque handle (``Pilot-XXXX``) derived deterministically from the
  id via a hash — never random, so the same pilot keeps the same handle across a
  whole report;
* ``include_named_pilots=False`` (the default) strips named pilots — their handle
  never enters the rehydration map, so the rendered report stays anonymous;
* a recognition opt-out (``pilots.PilotPreference.public_recognition`` off) is
  honoured here too: those pilots are always stripped, regardless of the flags.

The Django lookups (the opt-out id set) are resolved by the caller (``snapshot.py``)
and passed in via ``cfg`` so this module imports nothing from Django and is testable
in isolation. :func:`rehydrate` maps handles back to real names for the rendered
(internal-only) report.
"""
from __future__ import annotations

import hashlib
from typing import Any

# Field names under which a pilot node may carry a display name.
_NAME_FIELDS = ("name", "character_name", "pilot", "pilot_name")


def handle_for(character_id: int | str) -> str:
    """Stable opaque handle for a character id — ``Pilot-`` + 4 hex of its SHA1.

    Deterministic (no randomness) so a given pilot maps to the same handle every
    time, which is what lets :func:`rehydrate` reverse it in the rendered report.
    """
    digest = hashlib.sha1(str(character_id).encode()).hexdigest()  # noqa: S324 - label, not security
    return f"Pilot-{digest[:4].upper()}"


def pseudonymize_snapshot(slices: dict, cfg: dict) -> tuple[dict, dict]:
    """Return ``(slices_with_handles, handle_map)`` per doc 04 §5.

    ``cfg`` keys:
      * ``pseudonymize_pilots`` (bool, default True) — replace names with handles;
      * ``include_named_pilots`` (bool, default False) — when False, named pilots
        are stripped (handle present, but never mapped back);
      * ``optout_ids`` (iterable of character ids) — owners who opted out of public
        recognition; always stripped.

    ``handle_map`` is ``{handle: real_name}`` for the rehydrator and contains an
    entry ONLY for pilots that are both included *and* pseudonymised.
    """
    pseudo = bool(cfg.get("pseudonymize_pilots", True))
    include_named = bool(cfg.get("include_named_pilots", False))
    optout = {str(c) for c in (cfg.get("optout_ids") or ())}
    handle_map: dict[str, str] = {}

    def _apply_pilot(node: dict, cid: str) -> dict:
        name_key = next((k for k in _NAME_FIELDS if node.get(k)), None)
        if name_key is None:
            return node
        real = node[name_key]
        handle = handle_for(cid)
        if cid in optout or not include_named:
            # Anonymous: an opaque handle that is NEVER mapped back to a real name.
            node[name_key] = handle
        elif pseudo:
            node[name_key] = handle
            handle_map[handle] = real
        # else (include_named and not pseudo): keep the real name as-is.
        return node

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            cid = node.get("character_id")
            if cid is not None:
                node = _apply_pilot(dict(node), str(cid))
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(slices), handle_map


def rehydrate(text: str, handle_map: dict) -> str:
    """Replace opaque handles with the real names in a rendered report (doc 04 §5)."""
    if not text or not handle_map:
        return text
    # Longest handles first so no handle that is a prefix of another is half-replaced.
    for handle in sorted(handle_map, key=len, reverse=True):
        text = text.replace(handle, handle_map[handle])
    return text
