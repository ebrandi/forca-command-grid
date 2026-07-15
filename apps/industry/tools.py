"""Industry Center tools: dashboard, guide, calculator, invention, chain, jobs.

These are read-only planning surfaces on top of the domain services (calc /
invention / chain / availability). Members only. Items are always chosen through
the shared autocomplete (``?type_id=`` from ``components/_type_picker.html``) so a
pilot never types a numeric id by hand. Nothing here persists — the wizard/plan
CRUD lives in :mod:`views`.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from apps.market.pricing import price_for
from apps.sde.models import SdeDecryptor, SdeType
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import calc, chain, invention
from .models import IndustryProject, IndustryProjectItem
from .services import compute_project_bom, effective_rates


def _resolve_type(request: HttpRequest) -> SdeType | None:
    """Resolve the ``?type_id=`` picked through the autocomplete, if valid."""
    raw = (request.GET.get("type_id") or "").strip()
    if raw.isdigit():
        return SdeType.objects.filter(type_id=int(raw)).first()
    return None


def _int(request, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.GET.get(key, default))))
    except (TypeError, ValueError):
        return default


@login_required
@role_required(rbac.ROLE_MEMBER)
def industry_home(request: HttpRequest) -> HttpResponse:
    """The Industry Center landing: tools, my plans, and a demand/job pulse."""
    from apps.erp.models import CharacterIndustryJob, CorpIndustryJob

    mine = (
        IndustryProject.objects.filter(assigned_to=request.user, is_archived=False)
        .order_by("-updated_at")[:6]
    )
    char_ids = list(request.user.characters.values_list("character_id", flat=True))
    my_jobs = CharacterIndustryJob.objects.filter(
        character_id__in=char_ids, status__in=("active", "paused")
    ).count()
    return render(request, "industry/home.html", {
        "mine": mine,
        "active_plans": IndustryProject.objects.filter(is_archived=False).count(),
        "corp_jobs": CorpIndustryJob.objects.filter(status__in=("active", "paused")).count(),
        "my_jobs": my_jobs,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def corp_demand(request: HttpRequest) -> HttpResponse:
    """What the corp needs built: doctrine-supply shortfalls, ready to become plans."""
    from django.core.cache import cache

    from apps.doctrines.supply import corp_priority_list

    try:
        sets = max(1, min(100, int(request.GET.get("sets", 10))))
    except (TypeError, ValueError):
        sets = 10
    # Corp-wide aggregate — identical for every viewer; cache briefly so a page load
    # never recomputes the whole doctrine sweep.
    cache_key = f"industry:demand:{sets}"
    rows = cache.get(cache_key)
    if rows is None:
        rows = corp_priority_list(sets=sets, limit=40)
        cache.set(cache_key, rows, 300)
    return render(request, "industry/demand.html", {"rows": rows, "sets": sets})


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def plan_from_demand(request: HttpRequest) -> HttpResponse:
    """Turn one corp-demand line into a tracked production plan (source-tagged)."""
    raw = (request.POST.get("type_id") or "").strip()
    stype = SdeType.objects.filter(type_id=int(raw)).first() if raw.isdigit() else None
    if stype is None:
        messages.error(request, _("Pick a valid item from the demand list."))
        return redirect("industry:demand")
    try:
        quantity = max(1, int(request.POST.get("quantity") or 1))
    except (TypeError, ValueError):
        quantity = 1
    project = IndustryProject.objects.create(
        name=(_("Supply: %(item)s") % {"item": stype.name})[:200],
        objective_type=IndustryProject.Objective.STOCK,
        status=IndustryProject.Status.ACTIVE, source=IndustryProject.Source.DOCTRINE_SUPPLY,
        created_by=request.user, assigned_to=request.user,
    )
    IndustryProjectItem.objects.create(
        project=project, type_id=stype.type_id, product_name=stype.name, quantity=quantity,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
    )
    compute_project_bom(project)
    audit_log(request.user, "industry.plan_from_demand", target_type="industry_project",
              target_id=str(project.id), metadata={"type_id": stype.type_id, "quantity": quantity},
              ip=client_ip(request))
    messages.success(
        request,
        _("Plan created for %(quantity)s× %(item)s.")
        % {"quantity": quantity, "item": stype.name},
    )
    return redirect("industry:detail", pk=project.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def plan_from_job(request: HttpRequest) -> HttpResponse:
    """Turn an imported ESI job's product into a tracked production plan."""
    raw = (request.POST.get("type_id") or "").strip()
    stype = SdeType.objects.filter(type_id=int(raw)).first() if raw.isdigit() else None
    if stype is None:
        messages.error(request, _("Couldn't resolve that job's product."))
        return redirect("industry:jobs")
    # The imported job reports blueprint *runs*; the plan wants finished *units*
    # (runs × output-per-run — ammo/charges/drones yield many per run).
    try:
        runs = max(1, int(request.POST.get("quantity") or 1))
    except (TypeError, ValueError):
        runs = 1
    recipe = calc.bom.buildable_recipe(stype.type_id)
    output_per_run = recipe.output_quantity if recipe else 1
    quantity = runs * output_per_run
    project = IndustryProject.objects.create(
        name=(_("Build: %(item)s") % {"item": stype.name})[:200],
        objective_type=IndustryProject.Objective.BUILD,
        status=IndustryProject.Status.ACTIVE, source=IndustryProject.Source.ESI_JOB,
        created_by=request.user, assigned_to=request.user,
    )
    IndustryProjectItem.objects.create(
        project=project, type_id=stype.type_id, product_name=stype.name, quantity=quantity,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
    )
    compute_project_bom(project)
    audit_log(request.user, "industry.plan_from_job", target_type="industry_project",
              target_id=str(project.id), metadata={"type_id": stype.type_id, "quantity": quantity},
              ip=client_ip(request))
    messages.success(
        request,
        _("Plan created for %(quantity)s× %(item)s.")
        % {"quantity": quantity, "item": stype.name},
    )
    return redirect("industry:detail", pk=project.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
def guide(request: HttpRequest) -> HttpResponse:
    """The didactic 'how EVE industry works' onboarding page (static)."""
    return render(request, "industry/guide.html", {})


# Structure + rig material-efficiency presets (4.12): the extra material reduction an
# Engineering Complex with material rigs gives, on top of the blueprint ME. Values are the
# standard community figures (rig bonus × security multiplier).
# Only the label (element [1]) is translated: element [0] is the code value that
# round-trips through ``?structure=`` and is string-compared in calculator.html.
STRUCTURE_PRESETS = [
    ("none", gettext_lazy("NPC station (no bonus)"), 0.0),
    ("raitaru_t1", gettext_lazy("Raitaru · T1 ME rig (highsec)"), 0.020),      # 2.0% × 1.0
    ("raitaru_t2", gettext_lazy("Raitaru · T2 ME rig (highsec)"), 0.024),      # 2.4% × 1.0
    ("ec_t1_lowsec", gettext_lazy("Azbel/Sotiyo · T1 ME rig (lowsec)"), 0.038),  # 2.0% × 1.9
    ("ec_t2_lowsec", gettext_lazy("Azbel/Sotiyo · T2 ME rig (lowsec)"), 0.0456),  # 2.4% × 1.9
    ("ec_t2_null", gettext_lazy("Azbel/Sotiyo · T2 ME rig (null/WH)"), 0.0504),   # 2.4% × 2.1
]
_STRUCTURE_BONUS = {key: bonus for key, _label, bonus in STRUCTURE_PRESETS}


@login_required
@role_required(rbac.ROLE_MEMBER)
def calculator(request: HttpRequest) -> HttpResponse:
    """Manufacturing calculator: materials, costs, job fee, profit — for any item."""
    stype = _resolve_type(request)
    runs = _int(request, "runs", 1, 1, 100000)
    me = _int(request, "me", 0, 0, 10)
    te = _int(request, "te", 0, 0, 20)
    structure = request.GET.get("structure", "none")
    if structure not in _STRUCTURE_BONUS:
        structure = "none"
    structure_bonus = _STRUCTURE_BONUS[structure]
    strategy = request.GET.get("strategy", calc.bom.STRATEGY_BUILD_VS_BUY)
    if strategy not in (calc.bom.STRATEGY_BUILD_VS_BUY, calc.bom.STRATEGY_BUILD_TO_MINERALS):
        strategy = calc.bom.STRATEGY_BUILD_VS_BUY
    fold_invention = request.GET.get("invent") == "1"

    rates = effective_rates(None)
    estimate = inv_plan = None
    if stype is not None:
        inv_plan = invention.plan(stype.type_id)
        inv_cost = inv_plan["cost_per_run"] if (fold_invention and inv_plan) else None
        estimate = calc.manufacturing_estimate(
            stype.type_id, runs=runs, me=me, te=te, structure_bonus=structure_bonus,
            strategy=strategy,
            system_cost_index=rates["system_cost_index"], facility_tax=rates["facility_tax"],
            sales_tax=rates["sales_tax"], broker_fee=rates["broker_fee"],
            invention_cost_per_unit=inv_cost,
        )
    return render(request, "industry/calculator.html", {
        "stype": stype, "runs": runs, "me": me, "te": te, "strategy": strategy,
        "structure": structure, "structure_presets": STRUCTURE_PRESETS,
        "estimate": estimate, "inv_plan": inv_plan, "fold_invention": fold_invention,
        "rates": rates,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def invention_planner(request: HttpRequest) -> HttpResponse:
    """T2 invention planner: probability, expected cost/BPC, and build-vs-buy."""
    stype = _resolve_type(request)
    s1 = _int(request, "science_1", 4, 0, 5)
    s2 = _int(request, "science_2", 4, 0, 5)
    enc = _int(request, "encryption", 4, 0, 5)
    decryptor_raw = (request.GET.get("decryptor") or "").strip()
    decryptor_id = int(decryptor_raw) if decryptor_raw.isdigit() else None

    plan = comparison = None
    if stype is not None:
        plan = invention.plan(
            stype.type_id, science_1=s1, science_2=s2, encryption=enc,
            decryptor_type_id=decryptor_id,
        )
        if plan:
            comparison = _invention_comparison(stype.type_id, plan)
    return render(request, "industry/invention.html", {
        "stype": stype, "plan": plan, "comparison": comparison,
        "science_1": s1, "science_2": s2, "encryption": enc, "decryptor_id": decryptor_id,
        "decryptors": SdeDecryptor.objects.order_by("name"),
    })


def _invention_comparison(type_id: int, plan: dict) -> dict:
    """Compare buying the T2 item vs inventing + building it (per unit)."""
    buy = price_for(type_id)
    est = calc.manufacturing_estimate(type_id, runs=1)
    build_materials = (est["material_cost"] + est["install_fee"]) if est["buildable"] else None
    invent_per_unit = plan["cost_per_run"]
    total_invent_build = None
    if build_materials is not None and invent_per_unit is not None:
        total_invent_build = build_materials + invent_per_unit
    return {
        "buy_t2": buy,
        "invention_per_unit": invent_per_unit,
        "manufacture_per_unit": build_materials,
        "invent_and_build": total_invent_build,
        "cheaper": (
            "invent" if (total_invent_build is not None and buy and total_invent_build < buy)
            else "buy" if buy else "unknown"
        ),
    }


@login_required
@role_required(rbac.ROLE_MEMBER)
def chain_explorer(request: HttpRequest) -> HttpResponse:
    """Production-chain dependency tree for any buildable item."""
    stype = _resolve_type(request)
    quantity = _int(request, "quantity", 1, 1, 100000)
    me = _int(request, "me", 0, 0, 10)
    strategy = request.GET.get("strategy", chain.bom.STRATEGY_BUILD_TO_MINERALS)
    if strategy not in (chain.bom.STRATEGY_BUILD_VS_BUY, chain.bom.STRATEGY_BUILD_TO_MINERALS):
        strategy = chain.bom.STRATEGY_BUILD_TO_MINERALS
    tree = chain.chain_tree(stype.type_id, quantity, me=me, strategy=strategy) if stype else None
    return render(request, "industry/chain.html", {
        "stype": stype, "tree": tree, "quantity": quantity, "me": me, "strategy": strategy,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def blueprint_browser(request: HttpRequest) -> HttpResponse:
    """Browse owned blueprints (corp + personal) and inspect a recipe by name."""
    from apps.erp.models import Blueprint

    stype = _resolve_type(request)
    recipe = None
    if stype is not None:
        recipe = {
            "estimate": calc.manufacturing_estimate(stype.type_id, runs=1),
            "invention": invention.plan(stype.type_id),
        }
    char_ids = list(request.user.characters.values_list("character_id", flat=True))
    owned = list(
        Blueprint.objects.filter(source="esi")
        .order_by("owner_type", "type_id")[:400]
    )
    return render(request, "industry/blueprints.html", {
        "stype": stype, "recipe": recipe, "owned": owned, "my_char_ids": char_ids,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def job_tracker(request: HttpRequest) -> HttpResponse:
    """The production board: claimable corp build jobs, plus imported ESI jobs.

    This is the consolidated home for /erp/ (redirected here). The corp BuildJob
    queue keeps its execution flow (claim/deliver post to the erp: urls); the
    imported ESI jobs (corp + the pilot's own) show what's already in production.
    """
    from apps.erp import services as erp_services
    from apps.erp.models import BuildJob, CharacterIndustryJob, CorpIndustryJob

    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    queued = list(
        BuildJob.objects.filter(
            status__in=[BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED], owner__isnull=True
        )
    )
    for j in queued:
        erp_services.recheck_block(j)
    my_builds = list(
        BuildJob.objects.filter(owner=request.user).exclude(
            status__in=[BuildJob.Status.DELIVERED, BuildJob.Status.CANCELLED]
        )
    )

    def _job_row(j):
        return {
            "job": j,
            "mats": erp_services.job_materials(j),
            "can_manage": erp_services.can_manage(request.user, j, is_officer=is_officer),
        }

    char_ids = list(request.user.characters.values_list("character_id", flat=True))
    my_jobs = list(CharacterIndustryJob.objects.filter(character_id__in=char_ids).order_by("end_date"))
    corp_jobs = list(CorpIndustryJob.objects.order_by("end_date")[:200])

    plan_types = set(
        IndustryProject.objects.filter(assigned_to=request.user, is_archived=False)
        .values_list("items__type_id", flat=True)
    )
    for j in my_jobs:
        j.matched = j.product_type_id in plan_types

    from apps.sso.models import EveScopeGrant
    has_my_industry = EveScopeGrant.objects.filter(
        character__character_id__in=char_ids, feature_key="my_industry"
    ).exists()
    return render(request, "industry/jobs.html", {
        "queued": [_job_row(j) for j in queued],
        "my_builds": [_job_row(j) for j in my_builds],
        "is_officer": is_officer,
        "my_jobs": my_jobs, "corp_jobs": corp_jobs,
        "has_my_industry": has_my_industry,
        "unmatched": [j for j in my_jobs if not getattr(j, "matched", False) and j.is_active],
    })
