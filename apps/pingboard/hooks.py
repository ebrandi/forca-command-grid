"""Best-effort automation hooks for other services.

A service calls ``hooks.fire("srp.submitted", context=…, source_object_id=…)`` at a
lifecycle transition. This wraps ``automation.trigger`` so a Pingboard problem can never
break the caller's business action, and it is a cheap no-op until a matching rule is armed.
"""
from __future__ import annotations

import logging

log = logging.getLogger("forca.pingboard")


def fire(trigger_source: str, *, context: dict | None = None,
         source_object_id="", dedup_suffix="") -> list[int]:
    try:
        from .automation import trigger

        return trigger(trigger_source, context=context or {},
                       source_object_id=source_object_id, dedup_suffix=dedup_suffix)
    except Exception:  # noqa: BLE001 - an alert hook must never break the business action
        log.exception("pingboard automation hook %s failed", trigger_source)
        return []
