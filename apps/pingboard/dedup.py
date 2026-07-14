"""Shared "fire one deduped alert when a monitored state changes" helper.

Used by the Phase 2 leadership alerts (integration health, SRP SLA, structure
fuel/ADM, …). It encapsulates the pattern those all share:

* an on/off governance gate (``is_enabled(event_key)``);
* an ``AppSetting``-backed *problem-set signature*, so an unchanged state is a
  no-op and a return to healthy re-arms (a later recurrence alerts again);
* a per-firing microsecond stamp on the pingboard keys, so pingboard's own
  idempotency/duplicate guards never swallow a legitimate recurrence;
* burning the dedup slot **only after** pingboard accepts the alert, so a
  suppressed/failed emit retries next run rather than silently forgetting the
  problem (the reliability fix from the 2.2 review, applied once, here).

Callers compute the current problem set: ``problems`` is the list of stable
string keys that identify it (used for the signature) and ``body`` is the
human-readable digest to send. Empty ``problems`` means healthy.
"""
from __future__ import annotations

import hashlib
import logging

from django.utils import timezone

log = logging.getLogger("forca.pingboard")


def _signature(keys: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(keys)).encode("utf-8")).hexdigest()


def _emit(*, title: str, body: str, audience: dict, source_service: str,
          source_prefix: str, sig: str, template: str | None = None,
          context: dict | None = None) -> bool:
    # Per-firing microsecond stamp: two sequential firings are always distinct, so a
    # genuine recurrence is never swallowed by pingboard's idempotency/duplicate guard.
    stamp = int(timezone.now().timestamp() * 1_000_000)
    try:
        from apps.pingboard import services as pingboard

        alert = pingboard.emit_broadcast(
            category="custom",
            title=title,
            body=body,
            template=template,
            context=context,
            audience=audience,
            source_service=source_service,
            source_object_id=f"{source_prefix}:{sig[:16]}:{stamp}",
            idempotency_key=f"{source_service}:{source_prefix}:{sig[:16]}:{stamp}",
        )
        # None == pingboard suppressed it (globally disabled, or an in-window duplicate):
        # report not-sent so the caller does NOT burn the dedup slot and retries next run.
        return alert is not None
    except Exception:  # noqa: BLE001 — a notification fault must never break the scan
        log.exception("%s deduped alert failed", source_service)
        return False


def fire_on_change(*, event_key: str, sig_key: str, problems: list[str], title: str,
                   body: str, source_service: str, source_prefix: str,
                   audience: dict | None = None, template: str | None = None,
                   context: dict | None = None) -> dict:
    """Emit at most one alert per distinct problem set; a no-op when unchanged.

    ``event_key``   governance event (``is_enabled`` off-switch + audience override).
    ``sig_key``     ``AppSetting`` key holding the last-alerted signature (dedup state).
    ``problems``    stable string keys identifying the current problem set ([] = healthy).
    ``audience``    explicit audience dict; defaults to the event's console audience.
    ``template``    ``pingboard.messages.SCAFFOLDS`` key — the digest chrome, re-rendered per
                    recipient locale. ``context`` carries its raw values (the diagnostic lines);
                    ``title``/``body`` stay the frozen English audit columns.
    """
    from apps.admin_audit.models import AppSetting
    from apps.pingboard.notifications import is_enabled, resolve

    if not is_enabled(event_key):
        return {"status": "disabled"}

    stored = AppSetting.objects.filter(key=sig_key).first()
    if not problems:
        if stored is not None:
            stored.delete()  # recovered → reset dedup so a recurrence re-alerts
        return {"status": "ok"}

    sig = _signature(problems)
    if stored is not None and (stored.value or {}).get("sig") == sig:
        return {"status": "unchanged", "problems": len(problems)}

    if audience is None:
        audience = {"kind": resolve(event_key).get("audience") or "officer"}

    if not _emit(title=title, body=body, audience=audience, template=template, context=context,
                 source_service=source_service, source_prefix=source_prefix, sig=sig):
        return {"status": "alert_failed", "problems": len(problems)}

    AppSetting.objects.update_or_create(
        key=sig_key,
        defaults={"value": {"sig": sig, "at": timezone.now().isoformat(), "problems": problems}},
    )
    return {"status": "alerted", "problems": len(problems)}
