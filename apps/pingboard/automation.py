"""Automation rules — trigger → alert.

A service (Phase 6 wires the call sites) fires ``trigger("srp.submitted", context=…,
source_object_id=claim.pk, dedup_suffix="submitted")`` when something happens; every
enabled rule bound to that trigger source emits an alert, honouring its condition
filter, cooldown, per-window cap, expiry and dry-run flag. The caller controls the
idempotency granularity via ``source_object_id`` + ``dedup_suffix``.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from core.audit import audit_log

from .models import AutomationRule

log = logging.getLogger("forca.pingboard")


def trigger(trigger_source: str, *, context: dict | None = None,
            source_object_id="", dedup_suffix="") -> list[int]:
    """Fire every enabled rule bound to ``trigger_source``. Returns created alert ids."""
    context = context or {}
    fired: list[int] = []
    for rule in AutomationRule.objects.filter(trigger_source=trigger_source, enabled=True):
        alert = _fire(rule, context, str(source_object_id or ""), str(dedup_suffix or ""))
        if alert is not None:
            fired.append(alert.id)
    return fired


def _condition_met(rule: AutomationRule, context: dict) -> bool:
    """Evaluate a rule's condition against the context. Supports ``<field>_lt/_gt/_eq``
    numeric/equality comparisons and bare ``<field>: value`` equality."""
    for key, expected in (rule.condition or {}).items():
        if key.endswith("_lt"):
            if not _num(context.get(key[:-3])) < _num(expected):
                return False
        elif key.endswith("_gt"):
            if not _num(context.get(key[:-3])) > _num(expected):
                return False
        elif key.endswith("_gte"):
            if not _num(context.get(key[:-4])) >= _num(expected):
                return False
        elif key.endswith("_eq"):
            if context.get(key[:-3]) != expected:
                return False
        elif context.get(key) != expected:
            return False
    return True


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _within_cooldown(rule: AutomationRule) -> bool:
    if rule.cooldown_minutes and rule.last_fired_at:
        return timezone.now() < rule.last_fired_at + dt.timedelta(minutes=rule.cooldown_minutes)
    return False


def _window_cap_reached(rule: AutomationRule) -> bool:
    if not rule.max_per_window:
        return False
    from django.core.cache import cache

    return (cache.get(f"pb:auto:{rule.key}", 0) or 0) >= rule.max_per_window


def _window_incr(rule: AutomationRule) -> None:
    if not rule.max_per_window:
        return
    from django.core.cache import cache

    key = f"pb:auto:{rule.key}"
    if cache.add(key, 1, timeout=int(rule.window_minutes) * 60):
        return
    try:
        cache.incr(key)
    except ValueError:
        cache.add(key, 1, timeout=int(rule.window_minutes) * 60)


def _fire(rule: AutomationRule, context: dict, source_object_id: str, dedup_suffix: str):
    from . import services

    now = timezone.now()
    if rule.expires_at and rule.expires_at <= now:
        return None
    if not _condition_met(rule, context):
        return None
    if _within_cooldown(rule):
        return None
    if _window_cap_reached(rule):
        audit_log(None, "pingboard.automation.capped",
                  target_type="pingboard_automation_rule", target_id=rule.key)
        return None

    # A rule audience of {"kind": "context_user"} targets the affected pilot from the
    # trigger context (e.g. the SRP claimant) — how personal-outcome alerts are addressed.
    audience = rule.audience or None
    if isinstance(audience, dict) and audience.get("kind") == "context_user":
        audience = {"kind": "user", "id": context.get("target_user_id")}

    idem = ":".join(p for p in ("auto", rule.key, source_object_id, dedup_suffix) if p)
    alert = services.emit_alert(
        category=rule.category, priority=rule.priority,
        title=rule.title or rule.label, body=(rule.body or None),
        template=(rule.template.key if rule.template else None),
        context=context, audience=audience, channels=(rule.channels or None),
        source="automation", source_service=f"automation:{rule.trigger_source}",
        source_object_id=source_object_id, automation_rule=rule,
        idempotency_key=idem[:80], dry_run=rule.dry_run,
    )
    if alert is not None:
        rule.last_fired_at = now
        rule.save(update_fields=["last_fired_at", "updated_at"])
        _window_incr(rule)
        audit_log(None, "pingboard.automation.fired",
                  target_type="pingboard_automation_rule", target_id=rule.key,
                  metadata={"alert_id": alert.id, "trigger": rule.trigger_source})
    return alert


# --- threshold / scan-based triggers (swept by a beat) -----------------------
# These light up the API-driven "silent" sources (structures, moons, industry jobs)
# with alerts, in addition to the calendar events Phase 3 already syncs. Each fires a
# trigger per object with per-object idempotency, so the rule's condition + the emit
# idempotency key decide who actually gets alerted. All no-op unless a matching rule is
# enabled, so a corp that hasn't armed anything pays only a cheap "any enabled rule?" query.

def _has_rule(trigger_source: str) -> bool:
    return AutomationRule.objects.filter(trigger_source=trigger_source, enabled=True).exists()


def evaluate_threshold_rules() -> dict:
    out: dict = {}
    for label, fn in (("structure_fuel", _eval_structure_fuel),
                      ("moon_fracture", _eval_moon_fracture),
                      ("industry_complete", _eval_industry_complete)):
        try:
            out[label] = fn()
        except Exception:  # noqa: BLE001 - one source must not break the sweep
            log.exception("pingboard threshold sweep %s failed", label)
            out[label] = "error"
    return out


def _eval_structure_fuel() -> int:
    if not _has_rule("structure.fuel_low"):
        return 0
    from apps.corporation.models import CorpStructure

    now = timezone.now()
    fired = 0
    for s in CorpStructure.objects.filter(fuel_expires__isnull=False):
        days = (s.fuel_expires - now).total_seconds() / 86400
        ctx = {"structure_name": s.name or str(s.structure_id), "days_of_fuel": round(days, 1)}
        fired += len(trigger("structure.fuel_low", context=ctx, source_object_id=s.structure_id,
                             dedup_suffix=s.fuel_expires.strftime("%Y%m%d")))
    return fired


def _eval_moon_fracture() -> int:
    if not _has_rule("moon.fracture_ready"):
        return 0
    from apps.corporation.models import MoonExtraction

    now = timezone.now()
    fired = 0
    for ex in MoonExtraction.objects.filter(chunk_arrival__gte=now):
        hours = (ex.chunk_arrival - now).total_seconds() / 3600
        ctx = {"moon_name": ex.moon_name or ex.structure_name or str(ex.structure_id),
               "hours_to_fracture": round(hours, 1)}
        fired += len(trigger("moon.fracture_ready", context=ctx, source_object_id=ex.structure_id,
                             dedup_suffix=ex.chunk_arrival.strftime("%Y%m%d%H%M")))
    return fired


def _eval_industry_complete() -> int:
    if not _has_rule("industry.job_complete"):
        return 0
    from apps.erp.models import CorpIndustryJob

    now = timezone.now()
    fired = 0
    for j in CorpIndustryJob.objects.filter(status="active", end_date__isnull=False,
                                            end_date__gte=now):
        minutes = (j.end_date - now).total_seconds() / 60
        ctx = {"industry_job_name": f"job {j.job_id}", "minutes_to_complete": round(minutes)}
        fired += len(trigger("industry.job_complete", context=ctx, source_object_id=j.job_id,
                             dedup_suffix="complete"))
    return fired
