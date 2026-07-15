"""ESI colony import (Journey 7). Celery-only — never called from a web request.

Pulls a pilot's live colonies from ESI and stores a read-only, normalised snapshot.
IMPORTANT: ESI PI layout only updates when the pilot opens the colony in the EVE
client, so ``PiColony.last_update`` can lag reality. The UI always says so.

The seam is deliberately thin and self-contained so the "Import my colonies" button
can enqueue it and the rest of the module works with zero ESI configured.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError
from core.mixins import Source

from .issues_i18n import (
    EXTRACTOR_EXPIRED,
    FACTORY_NO_SCHEMATIC,
    NO_ROUTES,
    issue_label,
)
from .models import PiColony, PiMaterial, PiPlanetType, PiSchematic

# Opt-in pilot scope. Registered in settings.EVE_SSO_FEATURE_SCOPES + apps/sso/scopes.py.
PLANETS_SCOPE = "esi-planets.manage_planets.v1"


def _classify_pins(pins: list[dict]) -> dict:
    """Best-effort normalisation of a colony's pins into a human summary.

    We identify extractors (carry ``extractor_details``) and factories (carry a
    ``schematic_id``) from the payload shape; other pins (command center, launchpad,
    storage) are counted together since the SDE facility map isn't loaded here.
    """
    counts = {"extractor": 0, "factory": 0, "other": 0}
    extracting: dict[int, str] = {}
    schematics: dict[int, str] = {}
    issues: list[str] = []
    now = timezone.now()

    mat_names = dict(PiMaterial.objects.values_list("type_id", "name"))
    sch_names = dict(PiSchematic.objects.values_list("schematic_id", "name"))

    for pin in pins:
        details = pin.get("extractor_details")
        schem_id = pin.get("schematic_id")
        if details:
            counts["extractor"] += 1
            product = details.get("product_type_id")
            if product:
                extracting[product] = mat_names.get(product, str(product))
            expiry = pin.get("expiry_time")
            if expiry:
                dt = parse_datetime(expiry)
                if dt and dt < now:
                    issues.append(EXTRACTOR_EXPIRED)
        elif schem_id:
            counts["factory"] += 1
            schematics[schem_id] = sch_names.get(schem_id, f"schematic {schem_id}")
        else:
            counts["other"] += 1

    if counts["factory"] and not schematics:
        issues.append(FACTORY_NO_SCHEMATIC)
    return {
        "facilities": counts,
        "extracting": [{"type_id": t, "name": n} for t, n in extracting.items()],
        "schematics": [{"schematic_id": s, "name": n} for s, n in schematics.items()],
        "issues": sorted(set(issues)),
    }


log = logging.getLogger("forca.planetary")

_COLONY_EVENT_KEY = "planetary.colony_issue"


def _alert_colony_issues(colony, character) -> None:
    """PI-2 (3.5): DM the colony owner when a NEW issue-set appears (expired extractor,
    unrouted factory). At most one nudge per issue-set; a re-occurrence after a fix nudges
    again. The stored signature advances only when we actually alert or the issues clear, so
    a disabled event doesn't permanently swallow the nudge. Best-effort — never breaks import.
    """
    import hashlib

    try:
        issues = (colony.summary or {}).get("issues") or []
        sig = hashlib.sha256("\n".join(sorted(issues)).encode()).hexdigest()[:16] if issues else ""
        if sig == colony.alerted_sig:
            return  # same issue-set already handled (or both empty) — no re-nudge
        fired = False
        if issues and character.user_id:
            fired = _emit_colony_dm(colony, character, issues)
        if fired or not issues:  # advance only on a real alert or when the issues cleared
            colony.alerted_sig = sig
            colony.save(update_fields=["alerted_sig", "updated_at"])
    except Exception:  # noqa: BLE001 — the alert must never break the colony import
        log.exception("colony-issue alert failed (colony %s)", getattr(colony, "id", "?"))


def _emit_colony_dm(colony, character, issues) -> bool:
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_COLONY_EVENT_KEY):
        return False
    where = colony.solar_system_name or f"system {colony.solar_system_id}"
    planet = colony.planet_type_name or "planet"
    stamp = int(colony.fetched_at.timestamp()) if colony.fetched_at else 0
    # ``issues`` are stable codes; resolve them to human labels for the DM (English in the
    # Celery/audit context, verbatim for any unknown/legacy code).
    detail = "; ".join(issue_label(i) for i in issues[:3])
    body = f"Your PI colony on a {planet} in {where} needs attention: " + detail
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        pingboard.emit_broadcast(
            category=AlertCategory.INDUSTRY_JOB, title="PI colony needs attention", body=body,
            # Scaffold + raw context: the chrome re-renders per recipient locale; the colony's
            # planet/system names and the issue list stay raw. ``body`` is the English audit column.
            template="planetary.colony_issue",
            context={"planet_type": planet, "system_name": where,
                     "details": detail},
            audience={"kind": "user", "id": character.user_id},
            source_service="planetary", source_object_id=f"colony_issue:{colony.id}:{stamp}",
            idempotency_key=f"pi:colony_issue:{colony.id}:{stamp}",
        )
        return True
    except Exception:  # noqa: BLE001 — a notification must never break the colony import
        log.exception("colony-issue DM failed (colony %s)", colony.id)
        return False


def import_colonies(character, client: ESIClient | None = None) -> dict:
    """Sync one character's colonies. Returns a status dict; never raises."""
    client = client or ESIClient()
    try:
        access = get_valid_access_token(character, [PLANETS_SCOPE])
    except NoValidToken:
        return {"status": "no_scope", "character_id": character.character_id}

    cid = character.character_id
    try:
        colonies = client.get(f"/characters/{cid}/planets/", token=access).data or []
    except ESIError as exc:
        return {"status": "error", "detail": str(exc), "character_id": cid}

    from apps.sde.models import SdeSolarSystem

    slug_names = dict(PiPlanetType.objects.values_list("slug", "name"))
    imported = 0
    for col in colonies:
        planet_id = col.get("planet_id")
        if planet_id is None:
            continue
        system_id = col.get("solar_system_id")
        system_name = ""
        system = SdeSolarSystem.objects.filter(system_id=system_id).first()
        if system:
            system_name = system.name

        planet_type_slug = (col.get("planet_type") or "").lower()
        last_update = parse_datetime(col.get("last_update")) if col.get("last_update") else None

        summary = {"links": 0, "routes": 0}
        try:
            detail = client.get(f"/characters/{cid}/planets/{planet_id}/", token=access).data or {}
            summary.update(_classify_pins(detail.get("pins", [])))
            summary["links"] = len(detail.get("links", []))
            summary["routes"] = len(detail.get("routes", []))
            if not summary["routes"]:
                summary.setdefault("issues", []).append(NO_ROUTES)
        except ESIError:
            summary["detail_error"] = True

        colony, _ = PiColony.objects.update_or_create(
            character=character, planet_id=planet_id,
            defaults={
                "planet_type_id": None,
                "planet_type_name": slug_names.get(planet_type_slug, planet_type_slug.title()),
                "solar_system_id": system_id,
                "solar_system_name": system_name,
                "upgrade_level": col.get("upgrade_level") or 0,
                "num_pins": col.get("num_pins") or 0,
                "last_update": last_update,
                "summary": summary,
                "source": Source.ESI_CHAR,
                "as_of": last_update or timezone.now(),
                "fetched_at": timezone.now(),
            },
        )
        _alert_colony_issues(colony, character)  # PI-2 (3.5): nudge the owner on a new issue
        imported += 1

    # Drop colonies the pilot no longer has.
    kept = [c.get("planet_id") for c in colonies if c.get("planet_id") is not None]
    PiColony.objects.filter(character=character).exclude(planet_id__in=kept).delete()
    return {"status": "ok", "imported": imported, "character_id": cid}


def colonies_for_user(user):
    return (
        PiColony.objects.filter(character__user=user)
        .select_related("character")
        .order_by("solar_system_name", "planet_id")
    )
