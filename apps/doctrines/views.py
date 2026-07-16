"""Doctrine views: library, detail, readiness, fit export."""
from __future__ import annotations

from datetime import UTC
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import pilots, rbac
from core.audit import audit_log, client_ip

from .browse import readiness_sort_key
from .fitparser import export_eft
from .models import Doctrine, DoctrineDisplayConfig, DoctrineFit
from .services import character_readiness, readiness_summary_for_character


def _paginate(request: HttpRequest, items: list, per_page: int):
    """Page ``items`` and return ``(page_obj, base_querystring)``.

    ``base_querystring`` is every current GET param except ``page`` (so filters,
    sort and the selected pilot survive page navigation, and changing a filter —
    which omits ``page`` — naturally resets to page 1)."""
    page_obj = Paginator(items, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page_obj, params.urlencode()


@login_required
def doctrine_list(request: HttpRequest) -> HttpResponse:
    # Who can see doctrines (exact fits + required skills) is the leadership-set
    # "Ships & doctrines" audience, enforced centrally by core.features'
    # FeatureGateMiddleware — corp-only by default, but openable to alliance/public.
    from .library import build_library

    # Whose readiness/training plan? The pilot's own characters only; default to
    # the main, switchable via ?character_id=.
    characters = list(request.user.characters.all())
    char_id = request.GET.get("character_id")
    character = None
    if char_id:
        character = next((c for c in characters if str(c.character_id) == char_id), None)
    # An explicit ?character= wins (the pilot picker on the page); otherwise the pilot the
    # user is currently flying — never the account's main, which may not even be in the corp.
    character = character or pilots.acting_pilot(request.user)
    has_skills = bool(
        character and character.skill_snapshots.filter(is_latest=True).exists()
    )

    lib = build_library(character, has_skills=has_skills)
    rows = lib["rows"]

    # --- Filters (applied to the list only; the stats stay a full-library view) ---
    q = (request.GET.get("q") or "").strip()
    f_category = (request.GET.get("category") or "").strip()
    f_hull = (request.GET.get("hull") or "").strip()
    f_role = (request.GET.get("role") or "").strip()
    f_fly = (request.GET.get("fly") or "").strip()
    ql = q.lower()

    def keep(r: dict) -> bool:
        d = r["doctrine"]
        if ql and ql not in d.name.lower() and ql not in (d.description or "").lower():
            return False
        if f_category and str(r["category_id"]) != f_category:
            return False
        if f_hull and f_hull not in r["hull_classes"]:
            return False
        if f_role and f_role not in r["roles"]:
            return False
        if f_fly == "yes" and r["status"] not in ("optimal", "viable"):
            return False
        if f_fly == "no" and r["status"] != "not_ready":
            return False
        return True

    filtered = [r for r in rows if keep(r)]

    # Default order: what this pilot can fly first, then closest to flying. Without
    # a skill snapshot keep the library's priority/name order (build_library's sort).
    if has_skills:
        filtered.sort(key=lambda r: readiness_sort_key(r["status"], r.get("missing_count"), r["doctrine"].name))

    per_page = DoctrineDisplayConfig.active().effective_per_page()
    page_obj, base_qs = _paginate(request, filtered, per_page)

    # D5: htmx filter requests get just the results fragment (same context).
    template = "doctrines/_list_results.html" if request.headers.get("HX-Request") else "doctrines/list.html"
    return render(request, template, {
        "rows": page_obj.object_list,
        "page_obj": page_obj,
        "base_qs": base_qs,
        "total_shown": len(filtered),
        "total_all": len(rows),
        "characters": characters,
        "character": character,
        "has_character": character is not None,
        "has_skills": has_skills,
        "categories": lib["categories"],
        "hull_classes": lib["hull_classes"],
        "roles": lib["roles"],
        "q": q,
        "f_category": f_category,
        "f_hull": f_hull,
        "f_role": f_role,
        "f_fly": f_fly,
        "active_filters": any([q, f_category, f_hull, f_role, f_fly]),
        "stats": lib["stats"],
        "readiness": lib["readiness"],
        "priority": lib["priority"],
        "headline": lib["headline"],
    })


@login_required
@rbac.role_required(rbac.ROLE_OFFICER)
def coverage_dashboard(request: HttpRequest) -> HttpResponse:
    """DOC-2 (2.5): per-doctrine optimal/viable/not-ready pilot counts for leadership —
    one glance at fleet-composition capacity, priority doctrines first. Cached."""
    from apps.sso.models import EveCharacter

    from .services import corp_doctrine_coverage

    characters = list(EveCharacter.objects.filter(is_corp_member=True))
    audit_log(request.user, "doctrines.coverage_viewed",
              metadata={"members": len(characters)}, ip=client_ip(request))
    rows = corp_doctrine_coverage(characters)
    return render(request, "doctrines/coverage.html", {
        "rows": rows,
        "member_count": len(characters),
    })


@login_required
def doctrine_ships(request: HttpRequest) -> HttpResponse:
    """A flat, filterable browser of every doctrine ship: by hull class, role, and
    whether the pilot can fly it (or how close they are).

    This is also the browse-and-order surface for ready-to-fly doctrine ships. Access is
    the leadership-set "Ships & doctrines" audience (enforced by FeatureGateMiddleware);
    when the Corp Store is enabled it additionally prices each fit and shows the order
    button (member-only management links stay hidden for non-member shoppers).
    """
    from .browse import STATUS_RANK, enriched_fits, filter_options

    characters = list(request.user.characters.all()) if request.user.is_authenticated else []
    char_id = request.GET.get("character_id")
    character = None
    if char_id:
        character = next((c for c in characters if str(c.character_id) == char_id), None)
    # An explicit ?character= wins (the pilot picker on the page); otherwise the pilot the
    # user is currently flying — never the account's main, which may not even be in the corp.
    character = character or pilots.acting_pilot(request.user)

    # When the Corp Store is enabled the Shipyard also prices each fit (Jita sell ×
    # doctrine markup) and derives its availability (SHIP-1), so it is the one place
    # to browse and order a doctrine ship.
    from apps.store.services import current_audience

    store_enabled = current_audience() != "disabled"
    markup = None
    markup_pct = 0
    policy = None
    if store_enabled:
        from apps.store.models import ShipyardPolicy
        from apps.store.services import active_config

        cfg = active_config()
        markup = cfg.doctrine_markup
        markup_pct = int(round((float(cfg.doctrine_markup) - 1) * 100))
        policy = ShipyardPolicy.active()

    rows = enriched_fits(character, price_markup=markup, with_availability=store_enabled)
    options = filter_options(rows)

    hull = request.GET.get("hull", "")
    role = request.GET.get("role", "")
    fly = request.GET.get("fly", "")
    avail = request.GET.get("avail", "")
    loc = request.GET.get("loc", "")
    sort = request.GET.get("sort", "closest" if character else "name")

    # Delivery-location filter options (from availability, before filtering).
    locations = []
    if store_enabled:
        seen = {}
        for r in rows:
            a = r["availability"]
            if a is not None and a.location is not None:
                seen[a.location.pk] = a.location
        locations = sorted(seen.values(), key=lambda location: location.name.lower())

    if store_enabled and policy is not None:
        if not policy.show_unavailable:
            # Leadership chose to hide what can't be had right now.
            rows = [r for r in rows if r["availability"] is None
                    or r["availability"].can_order]
        if policy.available_only_default and "avail" not in request.GET:
            avail = "now"

    if hull:
        rows = [r for r in rows if r["hull_class"] == hull]
    if role:
        rows = [r for r in rows if r["role"] == role]
    if fly == "yes":
        rows = [r for r in rows if r["can_fly"]]
    elif fly == "no":
        rows = [r for r in rows if r["status"] == "not_ready"]
    elif fly == "close":
        rows = [r for r in rows if r["status"] == "not_ready" and (r["missing_count"] or 0) <= 3]
    if store_enabled and avail:
        def _state(r):
            return r["availability"].state if r["availability"] else ""
        if avail == "now":
            rows = [r for r in rows if _state(r) in ("ready", "limited")]
        elif avail in ("ready", "limited", "backorder", "unavailable", "not_offered"):
            rows = [r for r in rows if _state(r) == avail]
    if store_enabled and loc:
        rows = [
            r for r in rows
            if r["availability"] is not None and r["availability"].location is not None
            and str(r["availability"].location.pk) == loc
        ]

    _avail_rank = {"ready": 0, "limited": 1, "backorder": 2, "unavailable": 3, "not_offered": 4}

    if sort == "closest":
        # Fly-optimal first, then fly-viable, then not-ready by fewest skills missing.
        rows.sort(key=lambda r: readiness_sort_key(r["status"], r["missing_count"], r["ship_name"]))
    elif sort == "class":
        from .hulls import CLASS_ORDER
        rows.sort(key=lambda r: (CLASS_ORDER.index(r["hull_class"]), r["ship_name"]))
    elif sort == "ready":
        rows.sort(key=lambda r: (-STATUS_RANK[r["status"]], r["ship_name"]))
    elif sort == "availability" and store_enabled:
        rows.sort(key=lambda r: (
            _avail_rank.get(r["availability"].state, 9) if r["availability"] else 9,
            -(r["availability"].atp if r["availability"] else 0),
            r["ship_name"],
        ))
    elif sort == "fastest" and store_enabled:
        # In stock first (biggest ATP first), then backorders by soonest estimate.
        from datetime import datetime

        far = datetime(9999, 1, 1, tzinfo=UTC)

        def _fastest(r):
            a = r["availability"]
            if a is None:
                return (2, far, 0, r["ship_name"])
            if a.atp > 0:
                return (0, far, -a.atp, r["ship_name"])
            if a.state == "backorder":
                return (1, a.eta or far, 0, r["ship_name"])
            return (2, far, 0, r["ship_name"])
        rows.sort(key=_fastest)
    elif sort == "price" and store_enabled:
        rows.sort(key=lambda r: (r["unit_price"] is None, r["unit_price"] or 0, r["ship_name"]))
    else:
        rows.sort(key=lambda r: (r["doctrine"], r["fit_name"]))

    per_page = DoctrineDisplayConfig.active().effective_per_page()
    page_obj, base_qs = _paginate(request, rows, per_page)

    return render(request, "doctrines/ships.html", {
        "rows": page_obj.object_list, "count": len(rows),
        "page_obj": page_obj, "base_qs": base_qs,
        "characters": characters, "character": character,
        "hull_classes": options["hull_classes"], "roles": options["roles"],
        "hull": hull, "role": role, "fly": fly, "sort": sort,
        "avail": avail, "loc": loc, "locations": locations,
        "has_character": character is not None,
        "store_priced": store_enabled and markup is not None,
        "doctrine_markup_pct": markup_pct,
        "waitlist_enabled": bool(policy and policy.waitlist_enabled),
    })


@login_required
def doctrine_detail(request: HttpRequest, pk: int) -> HttpResponse:
    # Viewing a doctrine (its fits + required skills) follows the "Ships & doctrines"
    # audience, enforced centrally by FeatureGateMiddleware — same as the library list.
    doctrine = get_object_or_404(
        Doctrine.objects.prefetch_related("fits__requirements", "fits__skill_requirements"), pk=pk
    )
    # Since the "Ships & doctrines" audience can open this to alliance/public viewers,
    # pass the real membership so the template hides member-only tools (supply/prep) for
    # non-members rather than rendering dead links.
    return render(request, "doctrines/detail.html", {
        "doctrine": doctrine, "is_member": rbac.has_role(request.user, rbac.ROLE_MEMBER),
    })


@login_required
def my_readiness(request: HttpRequest) -> HttpResponse:
    """Rank every doctrine by how close a member is to flying it (best next first).

    Members only see their OWN characters; pick among them with ?character_id=.
    """
    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        return render(request, "doctrines/forbidden.html", status=403)

    characters = list(request.user.characters.all())
    char_id = request.GET.get("character_id")
    character = None
    if char_id:
        character = next((c for c in characters if str(c.character_id) == char_id), None)
    character = character or next((c for c in characters if c.is_main), characters[0] if characters else None)

    summary = readiness_summary_for_character(character) if character else []
    near = sorted(
        (r for r in summary if r["status"] == "not_ready"),
        key=lambda r: len(r["missing_viable"]),
    )
    ready = [r for r in summary if r["status"] in ("viable", "optimal")]
    unknown = [r for r in summary if r["status"] == "unknown"]
    return render(
        request,
        "doctrines/my_readiness.html",
        {
            "character": character, "characters": characters,
            "near": near, "ready": ready, "unknown": unknown,
        },
    )


@login_required
def doctrine_prep(request: HttpRequest, pk: int) -> HttpResponse:
    """Pilot pre-fleet prep for a doctrine: can I fly it, what do I buy, cost.

    Members only, and only over their OWN characters and assets.
    """
    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        return render(request, "doctrines/forbidden.html", status=403)
    from .prep import doctrine_prep as build_prep

    doctrine = get_object_or_404(
        Doctrine.objects.prefetch_related("fits__skill_requirements"), pk=pk
    )
    characters = list(request.user.characters.all())
    char_id = request.GET.get("character_id")
    character = None
    if char_id:
        character = next((c for c in characters if str(c.character_id) == char_id), None)
    # An explicit ?character= wins (the pilot picker on the page); otherwise the pilot the
    # user is currently flying — never the account's main, which may not even be in the corp.
    character = character or pilots.acting_pilot(request.user)
    char_ids = [c.character_id for c in characters]
    fits = build_prep(character, doctrine, char_ids) if character else []
    return render(
        request,
        "doctrines/prep.html",
        {
            "doctrine": doctrine,
            "character": character,
            "characters": characters,
            "fits": fits,
            "total_cost": sum((f["missing_cost"] for f in fits), start=Decimal("0")),
        },
    )


@login_required
def doctrine_supply(request: HttpRequest, pk: int) -> HttpResponse:
    """Corp supply plan for a doctrine: target vs on-hand, buy-vs-build, tasks.

    Members can view the resulting shopping list; officers get task fan-out.
    """
    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        return render(request, "doctrines/forbidden.html", status=403)
    from .supply import corp_priority_list, supply_plan

    doctrine = get_object_or_404(Doctrine.objects.prefetch_related("fits"), pk=pk)
    try:
        sets = max(1, min(int(request.GET.get("sets", 10)), 1000))
    except ValueError:
        sets = 10
    return render(
        request,
        "doctrines/supply.html",
        {
            "plan": supply_plan(doctrine, sets),
            "doctrine": doctrine,
            "sets": sets,
            "priority": corp_priority_list(sets),
            "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        },
    )


@login_required
@require_POST
def supply_task(request: HttpRequest) -> HttpResponse:
    """Officer turns a supply shortfall line into a claimable task (idempotent)."""
    if not rbac.has_role(request.user, rbac.ROLE_OFFICER):
        return render(request, "doctrines/forbidden.html", status=403)
    from apps.tasks.models import Task

    type_id = (request.POST.get("type_id") or "").strip()
    title = (request.POST.get("title") or "").strip()
    action = request.POST.get("action") or "buy"
    task_type = {"build": Task.Type.BUILD, "buy": Task.Type.BUY}.get(action, Task.Type.BUY)
    if not (type_id and title):
        messages.error(request, _("Incomplete supply line."))
        return redirect("doctrines:list")
    related_type = "supply"
    if not Task.objects.filter(
        related_type=related_type, related_id=type_id,
        status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS],
    ).exists():
        Task.objects.create(
            type=task_type, title=title, is_open=True, status=Task.Status.OPEN,
            priority=8, created_by=request.user,
            related_type=related_type, related_id=type_id,
        )
        messages.success(request, _("Task created — open to claim."))
    else:
        messages.info(request, _("A task for that item is already open."))
    from core.redirects import safe_next
    return redirect(safe_next(request, request.POST.get("next"), "doctrines:list"))


@login_required
def fit_export(request: HttpRequest, fit_id: int) -> HttpResponse:
    """Return a fit as EFT text (members copy it straight into the game/Pyfa).

    Also open to Corp Store shoppers (alliance / public per the store audience) so a
    buyer can copy the fit of a ready-to-fly ship they can order on the Shipyard.
    """
    # Access follows the "Ships & doctrines" audience (FeatureGateMiddleware).
    fit = get_object_or_404(DoctrineFit, pk=fit_id)
    eft = fit.eft_text.strip() or export_eft(fit)
    return HttpResponse(eft, content_type="text/plain; charset=utf-8")


@login_required
def doctrine_readiness(request: HttpRequest, pk: int) -> HttpResponse:
    # Personal readiness (own characters; officers may inspect a member) is a member
    # tool — kept member-only even when the browse audience is opened to alliance/public,
    # consistent with my_readiness / doctrine_prep / doctrine_supply.
    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        return render(request, "doctrines/forbidden.html", status=403)
    doctrine = get_object_or_404(Doctrine.objects.prefetch_related("fits"), pk=pk)

    # Which character? Default to the user's main; officers may inspect a member.
    target_character_id = request.GET.get("character_id")
    character = None
    if target_character_id:
        # Officer viewing another member's readiness is audit-logged.
        from apps.sso.models import EveCharacter

        character = EveCharacter.objects.filter(character_id=target_character_id).first()
        if character and character.user_id != request.user.id:
            if not rbac.has_role(request.user, rbac.ROLE_OFFICER):
                return render(request, "doctrines/forbidden.html", status=403)
            audit_log(
                request.user,
                "member.skills.view",
                target_type="character",
                target_id=str(character.character_id),
                metadata={"doctrine_id": pk},
                ip=client_ip(request),
            )
    if character is None:
        character = pilots.acting_pilot(request.user)

    results = []
    if character:
        snapshot = character.skill_snapshots.filter(is_latest=True).first()
        for fit in doctrine.fits.all():
            results.append(character_readiness(character, fit, snapshot=snapshot))
    return render(
        request,
        "doctrines/readiness.html",
        {"doctrine": doctrine, "character": character, "results": results},
    )
