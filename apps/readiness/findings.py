"""Persisting engine findings into the ReadinessFinding risk register (Phase 2).

The engine providers emit in-memory ``Finding`` dataclasses each run;
:func:`upsert_findings` writes them to the durable register, deduped by
``(dimension_key, kpi_key, ref_type, ref_id)`` and updated in place so each row
carries an age (``first_seen``/``last_seen``). A finding whose gap clears (its key
isn't emitted this run) is marked ``resolved``; one whose gap returns is the same
row flipped back to ``open``. The owner is resolved from
``readiness.responsibilities`` so a generated task already routes to the right desk.
"""
from __future__ import annotations

from django.utils import timezone


def resolve_owner(dimension_key: str, kpi_key: str, responsibilities: dict) -> str:
    """Owner tag for a finding: kpi_owner → dimension_owner → unassigned (doc 04 §7)."""
    kpi_owner = responsibilities.get("kpi_owner") or {}
    dimension_owner = responsibilities.get("dimension_owner") or {}
    return kpi_owner.get(kpi_key) or dimension_owner.get(dimension_key) or ""


def upsert_findings(engine_findings, *, ran_dimensions=None) -> dict:
    """Upsert the emitted findings; resolve stale ones. Returns ``{upserted, resolved}``.

    ``ran_dimensions`` is the set of dimension keys whose provider actually executed
    this run. Only findings in those dimensions are eligible to be auto-resolved, so a
    provider that *raised* (and therefore emitted nothing) never wipes its dimension's
    risk register. ``None`` means "trust the emitted set" (every dimension ran).
    """
    from . import config as config_module
    from .models import ReadinessFinding

    responsibilities = config_module.get("responsibilities")
    now = timezone.now()
    seen_keys: set[tuple] = set()
    upserted = 0

    for f in engine_findings:
        key = (f.dimension_key, f.kpi_key, f.ref_type, f.ref_id)
        if key in seen_keys:
            continue  # two emissions share a dedupe key this run — keep the first
        seen_keys.add(key)
        detail = f.detail.get("text", "") if isinstance(f.detail, dict) else str(f.detail or "")
        # Seam B: persist the scaffold KEY + its plain JSON params alongside the English
        # prose. The prose column is written exactly as before (English, from the msgid
        # source), so English output and every legacy reader are unchanged; the key is what
        # lets a reader in another locale re-render the sentence. A provider that emits no
        # key writes "" / {} and its row simply renders its stored English forever.
        fields = {
            "severity": f.severity,
            "kind": f.kind,
            "title": (f.label or f.dimension_key)[:200],
            "title_key": f.label_key,
            "title_params": f.label_params or {},
            "detail": detail,
            "detail_key": f.detail_key,
            "detail_params": f.detail_params or {},
            "task_title_key": f.task_title_key,
            "task_title_params": f.task_title_params or {},
            "weight": f.weight,
            "score": f.score,
            "owner_tag": resolve_owner(f.dimension_key, f.kpi_key, responsibilities),
            "task_type": f.task_type,
            "task_title": f.task_title,
            "predicted_breach_at": f.predicted_breach_at,
            "last_seen": now,
        }
        obj, created = ReadinessFinding.objects.get_or_create(
            dimension_key=f.dimension_key, kpi_key=f.kpi_key,
            ref_type=f.ref_type, ref_id=f.ref_id,
            defaults={**fields, "first_seen": now, "status": ReadinessFinding.Status.OPEN},
        )
        if not created:
            for field, value in fields.items():
                setattr(obj, field, value)
            # A re-emitted finding that had cleared (RESOLVED) or been worked
            # (ACKNOWLEDGED) but whose gap still measures returns to OPEN, so it is
            # eligible for a fresh task (doc 12 §7.2/§7.3).
            if obj.status in (ReadinessFinding.Status.RESOLVED, ReadinessFinding.Status.ACKNOWLEDGED):
                obj.status = ReadinessFinding.Status.OPEN
            obj.save()
        upserted += 1

    # A finding still active but NOT emitted by a provider that actually ran has
    # cleared → resolve it. Findings in a dimension whose provider raised this run
    # are left untouched (provider isolation — a transient failure must not wipe the
    # register). Forecast findings are excluded: they're produced and resolved by the
    # separate ``forecast`` pass, not by provider emission.
    active = ReadinessFinding.objects.filter(
        status__in=[ReadinessFinding.Status.OPEN, ReadinessFinding.Status.ACKNOWLEDGED]
    ).exclude(kind=ReadinessFinding.Kind.FORECAST)
    if ran_dimensions is not None:
        active = active.filter(dimension_key__in=ran_dimensions)
    resolved = 0
    for obj in active:
        if (obj.dimension_key, obj.kpi_key, obj.ref_type, obj.ref_id) not in seen_keys:
            obj.status = ReadinessFinding.Status.RESOLVED
            obj.save(update_fields=["status"])
            resolved += 1

    return {"upserted": upserted, "resolved": resolved}
