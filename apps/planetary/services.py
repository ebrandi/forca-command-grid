"""Planetary Industry service layer: config singleton, plan lifecycle, permissions.

Business logic lives here, never in views. Nothing moves ISK or writes to the game.
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext as _

from core import rbac

from . import calc
from .models import (
    PiPlan,
    PiPlanetType,
    PiStatus,
    PiVisibility,
    PlanetaryConfig,
)


# --------------------------------------------------------------------------- #
# Config singleton (mirrors srp.services.active_program)
# --------------------------------------------------------------------------- #
def active_config() -> PlanetaryConfig:
    config = PlanetaryConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    if config is None:
        config = PlanetaryConfig.objects.create(name="Standard", is_active=True)
    return config


def module_enabled() -> bool:
    """The leadership master switch (the feature flag is checked separately)."""
    return active_config().enabled


def config_form_initial(config: PlanetaryConfig) -> dict:
    """Seed a new plan's economic fields from the corp config (shown in the wizard,
    still fully editable). Leadership sets the house defaults; pilots adjust per plan."""
    return {
        "market_region_id": config.default_market_region_id,
        "customs_export_tax": config.default_customs_export_tax,
        "customs_import_tax": config.default_customs_import_tax,
        "sales_tax": config.default_sales_tax,
        "broker_fee": config.default_broker_fee,
        "hauling_cost_per_m3": config.default_hauling_cost_per_m3,
        "corp_buyback_rate": config.corp_buyback_rate,
        "extraction_rate_per_hour": config.default_extraction_rate_per_hour,
        "visibility": config.default_visibility or PiVisibility.PRIVATE,
    }


def finalize_new_plan(plan: PiPlan) -> None:
    """Fill the derived region label from the chosen pricing region."""
    from .constants import hub_label
    plan.market_region_name = hub_label(plan.market_region_id)


# --------------------------------------------------------------------------- #
# Plan lifecycle
# --------------------------------------------------------------------------- #
def recompute(plan: PiPlan) -> dict:
    """Recost the plan from live prices and snapshot the result."""
    snapshot = calc.plan_economics(plan)
    plan.snapshot = snapshot
    plan.last_priced_at = timezone.now()
    plan.save(update_fields=["snapshot", "last_priced_at", "updated_at"])
    return snapshot


def duplicate_plan(plan: PiPlan, owner) -> PiPlan:
    """Clone a plan (and its planets) for ``owner`` as a fresh draft."""
    clone = PiPlan.objects.create(
        owner=owner,
        character_id=plan.character_id,
        character_name=plan.character_name,
        name=f"{plan.name} (copy)"[:200],
        goal=plan.goal,
        status=PiStatus.DRAFT,
        visibility=plan.visibility,
        system_id=plan.system_id,
        system_name=plan.system_name,
        region_id=plan.region_id,
        planet_count=plan.planet_count,
        market_region_id=plan.market_region_id,
        market_region_name=plan.market_region_name,
        customs_export_tax=plan.customs_export_tax,
        customs_import_tax=plan.customs_import_tax,
        sales_tax=plan.sales_tax,
        broker_fee=plan.broker_fee,
        hauling_cost_per_m3=plan.hauling_cost_per_m3,
        corp_buyback_rate=plan.corp_buyback_rate,
        extraction_rate_per_hour=plan.extraction_rate_per_hour,
        effort=plan.effort,
        risk=plan.risk,
        export_strategy=plan.export_strategy,
        notes=plan.notes,
    )
    for p in plan.planets.all():
        clone.planets.create(
            planet_type=p.planet_type, role=p.role,
            primary_material=p.primary_material, output_override=p.output_override, order=p.order,
        )
    return clone


def archive_plan(plan: PiPlan) -> None:
    plan.status = PiStatus.ARCHIVED
    plan.save(update_fields=["status", "updated_at"])


# --------------------------------------------------------------------------- #
# Permissions (own plans; leadership sees shared)
# --------------------------------------------------------------------------- #
def can_manage(user, plan: PiPlan) -> bool:
    """Edit/delete/duplicate: the owner, or an officer+."""
    return plan.owner_id == user.id or rbac.has_role(user, rbac.ROLE_OFFICER)


def can_view(user, plan: PiPlan) -> bool:
    if can_manage(user, plan):
        return True
    if plan.visibility == PiVisibility.CORP and rbac.has_role(user, rbac.ROLE_MEMBER):
        return True
    if plan.visibility == PiVisibility.LEADERSHIP and rbac.has_role(user, rbac.ROLE_OFFICER):
        return True
    return False


def plans_for_user(user):
    return PiPlan.objects.filter(owner=user).prefetch_related("planets")


def shared_plans(viewer):
    """Plans other pilots have shared that ``viewer`` may see (for leadership views)."""
    qs = PiPlan.objects.exclude(owner=viewer).exclude(status=PiStatus.ARCHIVED)
    if rbac.has_role(viewer, rbac.ROLE_OFFICER):
        return qs.filter(visibility__in=[PiVisibility.CORP, PiVisibility.LEADERSHIP])
    if rbac.has_role(viewer, rbac.ROLE_MEMBER):
        return qs.filter(visibility=PiVisibility.CORP)
    return qs.none()


def planet_types():
    return list(PiPlanetType.objects.all())


def reconcile_plan_colonies(plan) -> list[dict]:
    """PI-1 (3.15): match the plan owner's live imported colonies to each plan planet and flag
    drift — a missing colony, one pulling a different product than planned, or one with issues
    (the cause, e.g. "extractor expired"). Turns the planner into a live health monitor.

    Read-only + owner-scoped; empty when the owner has imported no colonies.
    """
    from collections import defaultdict

    from .esi import colonies_for_user

    colonies = list(colonies_for_user(plan.owner))
    if not colonies:
        return []
    by_type: dict[str, list] = defaultdict(list)
    for c in colonies:
        by_type[(c.planet_type_name or "").strip().lower()].append(c)

    rows = []
    for pp in plan.planets.select_related("planet_type", "primary_material").order_by("order"):
        matches = by_type.get((pp.planet_type.name or "").strip().lower(), [])
        # v1 health hint: the first same-type colony represents this planet; others (rare)
        # just bump extra_colonies. A per-plan colony binding is future work.
        colony = matches[0] if matches else None
        issues = ((colony.summary or {}).get("issues") if colony else None) or []
        extracting = {
            e.get("type_id")
            for e in ((colony.summary or {}).get("extracting") if colony else []) or []
        }
        expected = pp.primary_material.type_id if pp.primary_material_id else None
        wrong_product = bool(
            pp.role == "extract" and expected and extracting and expected not in extracting
        )
        if colony is None:
            drift = _("No live colony of this type imported yet.")
        elif issues:
            drift = issues[0]
        elif wrong_product:
            drift = _("Your live colony is pulling a different product than the plan expects.")
        else:
            drift = ""
        rows.append({
            "planet": pp,
            "colony": colony,
            "extra_colonies": max(0, len(matches) - 1),
            "issues": issues,
            "drift": drift,
            "ok": colony is not None and not issues and not wrong_product,
        })
    return rows
