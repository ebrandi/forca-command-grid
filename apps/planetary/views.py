"""Planetary Industry — pilot-facing views (Journeys 1–7).

A guided assistant, not a data-entry page: a didactic landing, a guided plan wizard,
plan cards with safe edit/duplicate/archive/recost, a chain explorer, a recommendation
panel, per-planet setup guidance and an opt-in ESI colony import.

Permissions: pilots manage their own plans; officers may view/manage shared plans.
All ESI work is enqueued to Celery — never run in the request.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import calc, chains, guide, recommend, services
from .constants import TIER_META, TIER_ORDER, TRADE_HUBS, hub_label
from .esi import PLANETS_SCOPE, colonies_for_user
from .forms import PiPlanForm
from .models import PiGoal, PiMaterial, PiPlan, PiPlanetRole, PiPlanetType, PiStatus
from .prices import PriceProvider


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _plan_card(plan: PiPlan) -> dict:
    """Summary data for a plan card from its last snapshot."""
    snap = plan.snapshot or {}
    totals = snap.get("totals", {})
    planets = list(plan.planets.all())
    tiers = sorted({p.primary_material.tier for p in planets if p.primary_material},
                   key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else 0)
    products = [p.primary_material.name for p in planets if p.primary_material]
    health = _health(plan, snap)
    return {
        "plan": plan,
        "planet_count": len(planets) or plan.planet_count,
        "planet_types": sorted({p.planet_type.name for p in planets}),
        "tiers": tiers,
        "products": products[:4],
        "net_day": totals.get("net_day"),
        "net_week": totals.get("net_week"),
        "net_month": totals.get("net_month"),
        "tax_burden_day": totals.get("tax_burden_day"),
        "complexity": snap.get("complexity", "—"),
        "confidence": snap.get("confidence", "—"),
        "health": health,
        "priced": not snap.get("missing_prices"),
    }


def _health(plan: PiPlan, snap: dict) -> tuple[str, str]:
    """A derived status badge (label, colour) on top of the persisted status."""
    if plan.status == PiStatus.ARCHIVED:
        return (_("Archived"), "faint")
    if not plan.planets.exists():
        return (_("Missing planets"), "loss")
    if not snap:
        return (_("Not costed"), "faint")
    if snap.get("missing_prices"):
        return (_("Missing prices"), "gold")
    if (snap.get("totals", {}).get("net_day") or 0) <= 0:
        return (_("Unprofitable"), "loss")
    return (_("Profitable"), "kill")


def _get_plan_for_view(user, pk: int) -> PiPlan:
    plan = get_object_or_404(PiPlan.objects.prefetch_related("planets__planet_type",
                                                             "planets__primary_material"), pk=pk)
    if not services.can_view(user, plan):
        raise Http404("No such plan.")
    return plan


def _get_plan_for_manage(user, pk: int) -> PiPlan:
    plan = get_object_or_404(PiPlan, pk=pk)
    if not services.can_manage(user, plan):
        raise PermissionDenied("Not your plan to manage.")
    return plan


def _parse_planets(request: HttpRequest) -> list[dict]:
    """Parse the wizard's dynamic planet rows (parallel arrays) into clean dicts."""
    types = {p.slug: p for p in PiPlanetType.objects.all()}
    valid_roles = {r.value for r in PiPlanetRole}
    material_ids = set(PiMaterial.objects.values_list("type_id", flat=True))
    slugs = request.POST.getlist("planet_type")
    roles = request.POST.getlist("planet_role")
    products = request.POST.getlist("planet_product")
    rows = []
    for i, slug in enumerate(slugs):
        slug = (slug or "").strip()
        if slug not in types:
            continue
        role = roles[i] if i < len(roles) else PiPlanetRole.EXTRACT
        if role not in valid_roles:
            role = PiPlanetRole.EXTRACT
        prod_raw = (products[i] if i < len(products) else "").strip()
        prod_id = int(prod_raw) if prod_raw.isdigit() and int(prod_raw) in material_ids else None
        rows.append({"planet_type": types[slug], "role": role, "product_id": prod_id, "order": i})
    return rows


def _onboarding_context() -> dict:
    return {
        "tier_meta": [(t, TIER_META[t]) for t in TIER_ORDER],
        "planet_types": list(PiPlanetType.objects.all()),
    }


# --------------------------------------------------------------------------- #
# Journey 1 + 3: landing (overview + onboarding teaser + my plans)
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def landing(request: HttpRequest) -> HttpResponse:
    config = services.active_config()
    plans = services.plans_for_user(request.user).exclude(status=PiStatus.ARCHIVED)
    cards = [_plan_card(p) for p in plans]
    archived = services.plans_for_user(request.user).filter(status=PiStatus.ARCHIVED).count()
    ctx = {
        "config": config,
        "enabled": config.enabled,
        "cards": cards,
        "archived_count": archived,
        "has_plans": bool(cards),
        **_onboarding_context(),
    }
    return render(request, "planetary/landing.html", ctx)


@login_required
@role_required(rbac.ROLE_MEMBER)
def learn(request: HttpRequest) -> HttpResponse:
    """Journey 1 — the didactic 'what is PI and why' page."""
    return render(request, "planetary/learn.html", _onboarding_context())


# --------------------------------------------------------------------------- #
# Journey 2: the guided wizard (create)
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def plan_create(request: HttpRequest) -> HttpResponse:
    config = services.active_config()
    if not config.enabled:
        messages.info(request, _("The Planetary Industry planner is currently disabled by leadership."))
        return redirect("planetary:landing")

    if request.method == "POST":
        form = PiPlanForm(request.POST, user=request.user)
        planet_rows = _parse_planets(request)
        errors = []
        if not planet_rows:
            errors.append(_("Add at least one planet — that's what the plan is built around."))
        for row in planet_rows:
            if row["role"] in (PiPlanetRole.FACTORY,) and not row["product_id"]:
                errors.append(_("Each factory planet needs a product to build — pick one."))
                break
        if form.is_valid() and not errors:
            plan = form.save(commit=False)
            plan.owner = request.user
            plan.save()
            for row in planet_rows:
                plan.planets.create(
                    planet_type=row["planet_type"], role=row["role"],
                    primary_material_id=row["product_id"], order=row["order"])
            services.recompute(plan)
            audit_log(request.user, "planetary.plan_create", target_type="pi_plan",
                      target_id=str(plan.id), metadata={"name": plan.name, "goal": plan.goal},
                      ip=client_ip(request))
            messages.success(
                request,
                _("Plan “%(name)s” created — here's your setup and profit estimate.") % {"name": plan.name},
            )
            return redirect("planetary:detail", pk=plan.pk)
        for err in errors:
            messages.error(request, err)
    else:
        form = PiPlanForm(user=request.user, initial=services.config_form_initial(config))

    return render(request, "planetary/wizard.html", _form_context(request, form))


@login_required
@role_required(rbac.ROLE_MEMBER)
def plan_edit(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_plan_for_manage(request.user, pk)
    if request.method == "POST":
        form = PiPlanForm(request.POST, instance=plan, user=request.user)
        planet_rows = _parse_planets(request)
        if form.is_valid() and planet_rows:
            plan = form.save()
            plan.planets.all().delete()
            for row in planet_rows:
                plan.planets.create(
                    planet_type=row["planet_type"], role=row["role"],
                    primary_material_id=row["product_id"], order=row["order"])
            services.recompute(plan)
            audit_log(request.user, "planetary.plan_update", target_type="pi_plan",
                      target_id=str(plan.id), ip=client_ip(request))
            messages.success(request, _("Plan updated and re-costed."))
            return redirect("planetary:detail", pk=plan.pk)
        if not planet_rows:
            messages.error(request, _("Keep at least one planet on the plan."))
    else:
        form = PiPlanForm(instance=plan, user=request.user)
    return render(request, "planetary/wizard.html", _form_context(request, form, plan=plan))


def _form_context(request, form, plan=None) -> dict:
    planet_types = list(PiPlanetType.objects.prefetch_related("resources__material"))
    # Serialisable catalogues for the Alpine planet builder.
    planet_catalogue = [
        # ``name`` and the resource names are CCP game data (English); ``best_for`` is our
        # prose and goes through the render-time seam. The dict key stays ``best_for`` so
        # the Alpine builder and its template are untouched.
        {"slug": p.slug, "name": p.name, "best_for": p.best_for_i18n,
         "resources": [m.name for m in p.resource_materials]}
        for p in planet_types
    ]
    material_catalogue = [
        {"type_id": m.type_id, "name": m.name, "tier": m.tier}
        for m in PiMaterial.objects.all()
    ]
    existing = []
    if plan is not None:
        existing = [
            {"slug": pp.planet_type.slug, "role": pp.role,
             "product_id": pp.primary_material_id or "",
             "product_name": pp.primary_material.name if pp.primary_material else ""}
            for pp in plan.planets.select_related("planet_type", "primary_material")
        ]
    return {
        "form": form, "plan": plan,
        "planet_catalogue": planet_catalogue,
        "material_catalogue": material_catalogue,
        "existing_planets": existing,
        "roles": [(r.value, r.label) for r in PiPlanetRole],
        "hubs": TRADE_HUBS,
        **_onboarding_context(),
    }


# --------------------------------------------------------------------------- #
# Journey 3: plan detail + management actions
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def plan_detail(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_plan_for_view(request.user, pk)
    graph = chains.build_graph()
    provider = PriceProvider(plan.market_region_id)
    economics = calc.plan_economics(plan, graph=graph, provider=provider)

    setup = guide.planet_setup(plan, graph)
    # The highest-tier product in the plan drives the "what you're building" chain view.
    primary = _headline_product(plan, graph)
    chain_tree = comparison = None
    if primary is not None:
        node = graph.requirements(primary, 1)
        chain_tree = _serialize_node(node, graph, provider) if node else None
        comparison = calc.refine_vs_sell(primary, provider, graph)

    ctx = {
        "plan": plan,
        "can_manage": services.can_manage(request.user, plan),
        "economics": economics,
        "setup": setup,
        "chain_tree": chain_tree,
        "comparison": comparison,
        "mistakes": guide.common_mistakes(),
        "checklist": guide.build_checklist(),
        "statuses": [(s.value, s.label) for s in PiStatus if s != PiStatus.ARCHIVED],
        # PI-1 (3.15): the owner's live colonies are personal (op-sec) data, kept self-scoped
        # like the /colonies/ page — only surface the reconciliation to the plan's own owner,
        # never to a corp/leadership viewer of the plan.
        "colony_reconciliation": (
            services.reconcile_plan_colonies(plan) if request.user.id == plan.owner_id else None
        ),
    }
    return render(request, "planetary/detail.html", ctx)


def _headline_product(plan, graph):
    best, best_idx = None, -1
    for pp in plan.planets.select_related("primary_material"):
        if pp.primary_material:
            idx = TIER_ORDER.index(pp.primary_material.tier) if pp.primary_material.tier in TIER_ORDER else 0
            if idx > best_idx:
                best, best_idx = pp.primary_material.type_id, idx
    return best


def _serialize_node(node, graph, provider) -> dict:
    return {
        "type_id": node.material.type_id,
        "name": node.material.name,
        "tier": node.material.tier,
        "quantity": float(node.quantity),
        "unit_price": float(provider.sell(node.material.type_id)),
        "is_raw": node.is_raw,
        "inputs": [_serialize_node(c, graph, provider) for c in node.inputs],
    }


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def plan_recalc(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_plan_for_manage(request.user, pk)
    services.recompute(plan)
    audit_log(request.user, "planetary.plan_recalc", target_type="pi_plan",
              target_id=str(plan.id), ip=client_ip(request))
    messages.success(request, _("Re-costed with the latest market prices."))
    return redirect("planetary:detail", pk=plan.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def plan_duplicate(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_plan_for_view(request.user, pk)
    clone = services.duplicate_plan(plan, request.user)
    services.recompute(clone)
    audit_log(request.user, "planetary.plan_duplicate", target_type="pi_plan",
              target_id=str(clone.id), metadata={"from": plan.id}, ip=client_ip(request))
    messages.success(request, _("Duplicated as “%(name)s”.") % {"name": clone.name})
    return redirect("planetary:detail", pk=clone.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def plan_status(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_plan_for_manage(request.user, pk)
    new_status = request.POST.get("status", "")
    valid = {s.value for s in PiStatus}
    if new_status in valid:
        plan.status = new_status
        plan.save(update_fields=["status", "updated_at"])
        audit_log(request.user, "planetary.plan_status", target_type="pi_plan",
                  target_id=str(plan.id), metadata={"status": new_status}, ip=client_ip(request))
        messages.success(request, _("Status set to %(status)s.") % {"status": plan.get_status_display()})
    return redirect("planetary:detail", pk=plan.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def plan_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Two-step safe delete: archive first, permanently delete only if already archived."""
    plan = _get_plan_for_manage(request.user, pk)
    if plan.status != PiStatus.ARCHIVED:
        services.archive_plan(plan)
        audit_log(request.user, "planetary.plan_archive", target_type="pi_plan",
                  target_id=str(plan.id), ip=client_ip(request))
        messages.success(
            request, _("“%(name)s” archived. Delete again to remove it permanently.") % {"name": plan.name}
        )
        return redirect("planetary:detail", pk=plan.pk)
    name = plan.name
    audit_log(request.user, "planetary.plan_delete", target_type="pi_plan",
              target_id=str(plan.id), metadata={"name": name}, ip=client_ip(request))
    plan.delete()
    messages.success(request, _("“%(name)s” permanently deleted.") % {"name": name})
    return redirect("planetary:landing")


# --------------------------------------------------------------------------- #
# Journey 4: chain explorer
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def explore(request: HttpRequest) -> HttpResponse:
    graph = chains.build_graph()
    provider = PriceProvider()

    material_id = request.GET.get("material")
    planet_slug = request.GET.get("planet")
    ctx = {"planet_types": list(PiPlanetType.objects.all()),
           "tiers": [(t, TIER_META[t]) for t in TIER_ORDER],
           "selected_material": None, "selected_planet": None}

    if material_id and material_id.isdigit():
        mat = graph.material(int(material_id))
        if mat:
            node = graph.requirements(mat.type_id, 1)
            ctx["selected_material"] = {
                "info": mat,
                "tree": _serialize_node(node, graph, provider) if node else None,
                "becomes": [graph.material(s.output_id) for s in graph.becomes(mat.type_id)],
                "planets_needed": graph.planet_cover(list(graph.raw_leaves(node))) if node else [],
                "comparison": calc.refine_vs_sell(mat.type_id, provider, graph),
            }
    elif planet_slug:
        pt = graph.planet_types.get(planet_slug)
        if pt:
            p0_ids = graph.resources_by_planet.get(planet_slug, [])
            ctx["selected_planet"] = {
                "planet": pt,
                "resources": [graph.material(t) for t in p0_ids],
                "reachable": graph.reachable_products(p0_ids),
            }

    # A tier browser (P0→P4) as the default view.
    by_tier = {}
    for m in graph.materials.values():
        by_tier.setdefault(m.tier, []).append(m)
    ctx["by_tier"] = [(t, TIER_META[t], sorted(by_tier.get(t, []), key=lambda m: m.name))
                      for t in TIER_ORDER]
    return render(request, "planetary/explore.html", ctx)


# --------------------------------------------------------------------------- #
# Journey 5: recommendations
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def recommend_view(request: HttpRequest) -> HttpResponse:
    config = services.active_config()
    graph = chains.build_graph()
    provider = PriceProvider(config.default_market_region_id)

    goal = request.GET.get("goal") or "max_profit"
    planet_slugs = [s for s in request.GET.getlist("planet") if s in graph.planet_types]
    items = recommend.recommend(config=config, graph=graph, provider=provider,
                                goal=goal or None, planet_slugs=planet_slugs or None, limit=8)
    ctx = {
        "config": config,
        "items": items,
        "goal": goal,
        "goals": [(g.value, g.label) for g in PiGoal],
        "planet_types": list(PiPlanetType.objects.all()),
        "selected_planets": planet_slugs,
        "region_name": hub_label(config.default_market_region_id),
    }
    return render(request, "planetary/recommend.html", ctx)


# --------------------------------------------------------------------------- #
# Journey 7: ESI colony import
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def colonies(request: HttpRequest) -> HttpResponse:
    chars = list(request.user.characters.all().order_by("-is_main", "name"))
    scoped_ids = set(
        request.user.characters.filter(
            scope_grants__scope=PLANETS_SCOPE, scope_grants__active=True
        ).values_list("character_id", flat=True)
    )
    ctx = {
        "colonies": list(colonies_for_user(request.user)),
        "characters": [{"char": c, "granted": c.character_id in scoped_ids} for c in chars],
        "any_granted": bool(scoped_ids),
        "scope_feature": "planetary_industry",
    }
    return render(request, "planetary/colonies.html", ctx)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def colonies_sync(request: HttpRequest) -> HttpResponse:
    from .tasks import sync_character_colonies

    queued = 0
    for char in request.user.characters.filter(
            scope_grants__scope=PLANETS_SCOPE, scope_grants__active=True).distinct():
        sync_character_colonies.delay(char.character_id)
        queued += 1
    if queued:
        messages.success(
            request, _("Colony sync queued for %(count)s pilot(s) — refresh in a minute.") % {"count": queued}
        )
    else:
        messages.info(request, _("Grant the Planetary Industry scope first, then sync your colonies."))
    audit_log(request.user, "planetary.colonies_sync", metadata={"queued": queued},
              ip=client_ip(request))
    return redirect("planetary:colonies")


# --------------------------------------------------------------------------- #
# JSON: PI material autocomplete
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_MEMBER)
def type_search(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip()
    tier = request.GET.get("tier") or ""
    qs = PiMaterial.objects.all()
    if tier in {"P0", "P1", "P2", "P3", "P4"}:
        qs = qs.filter(tier=tier)
    if q:
        qs = qs.filter(name__icontains=q)
    rows = [{"type_id": m.type_id, "name": m.name, "tier": m.tier} for m in qs[:20]]
    return JsonResponse(rows, safe=False)
