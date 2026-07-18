"""Tocha's Lab views — the fitting workspace.

Telemetry is ALWAYS computed server-side (the client never supplies numbers); the live
recompute endpoint is stateless and reuses the same engine adapter as a saved-fit render,
so an editor session and a shared page can never disagree. Every persistent action is
owner-checked server-side; public links resolve only by unguessable token.
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.sde.search import search_ships, search_types
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.features import feature_required

from . import services
from .models import Fit

_MAX_ITEMS = 300              # a fit can hold at most this many fitted entries
_MAX_PAYLOAD = 200_000        # bytes — bound an oversized items paste


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _skill_profile(request, source: str | None):
    """Resolve the skill profile to simulate: all-V, none, or a pilot's real skills."""
    source = (source or "pilot").strip()
    if source == "allv":
        return services.SkillProfile.omniscient(label=_("All V"))
    if source == "none":
        return services.SkillProfile.from_dict({}, label=_("Untrained"))
    user = request.user
    if source.isdigit():
        pilot = pilots.owned_pilot(user, int(source))
        if pilot:
            return services.pilot_skill_profile(pilot, label=pilot.name)
    pilot = pilots.acting_pilot(user)
    return services.pilot_skill_profile(pilot, label=getattr(pilot, "name", _("current")))


def _op_profile(request):
    data = request.POST if request.method == "POST" else request.GET
    mode = data.get("mode", "all_active")
    prop = data.get("prop", "1") not in ("0", "false", "")
    dmg = None
    if data.get("dmg_em") is not None:
        try:
            dmg = {k: float(data.get(f"dmg_{k}", 25)) for k in ("em", "thermal", "kinetic", "explosive")}
        except (TypeError, ValueError):
            dmg = None
    return services.operating_profile(mode=mode, propulsion=prop, damage=dmg)


def _parse_items(raw: str) -> list[dict]:
    if not raw or len(raw) > _MAX_PAYLOAD:
        raise ValueError("payload too large")
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) > _MAX_ITEMS:
        raise ValueError("invalid items")
    clean = []
    for it in data:
        if not isinstance(it, dict) or "type_id" not in it:
            continue
        clean.append({
            "type_id": int(it["type_id"]),
            "slot": str(it.get("slot", "low"))[:12],
            "state": str(it.get("state", "active"))[:12],
            "charge_type_id": int(it["charge_type_id"]) if it.get("charge_type_id") else None,
            "quantity": max(1, min(int(it.get("quantity", 1)), 5000)),
        })
    return clean


def _require_owner(request, pk) -> Fit:
    fit = get_object_or_404(Fit, pk=pk)
    if not fit.can_edit(request.user):
        raise Http404  # never reveal existence of a fit the user can't edit
    return fit


def _item_display(items: list[dict]) -> list[dict]:
    ids = {int(i["type_id"]) for i in items} | {int(i["charge_type_id"]) for i in items if i.get("charge_type_id")}
    names = services._type_names(ids)
    out = []
    for it in items:
        out.append({**it, "name": names.get(int(it["type_id"]), f"Type {it['type_id']}"),
                    "charge_name": names.get(int(it["charge_type_id"]), "") if it.get("charge_type_id") else ""})
    return out


# --------------------------------------------------------------------------- #
# List + create + import
# --------------------------------------------------------------------------- #
@login_required
@feature_required("tochas_lab")
def index(request):
    fits = list(Fit.objects.filter(owner=request.user, is_archived=False)
                .select_related("current_revision")[:100])
    ship_names = services._type_names({f.ship_type_id for f in fits})
    for f in fits:
        f.ship_name = ship_names.get(f.ship_type_id, f"Type {f.ship_type_id}")
    return render(request, "fitting/index.html", {"fits": fits, "brand": _("Tocha's Lab")})


@login_required
@feature_required("tochas_lab")
@require_POST
def create(request):
    from apps.sde.search import resolve_type
    ship = resolve_type(request.POST.get("ship", ""))
    if not ship:
        return render(request, "fitting/index.html",
                      {"fits": [], "error": _("Pick a ship hull to start a fit."),
                       "brand": _("Tocha's Lab")})
    fit = services.create_fit(request.user, name=request.POST.get("name") or ship.name,
                              ship_type_id=ship.type_id, items=[], origin="scratch")
    return redirect("fitting:detail", pk=fit.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def import_eft(request):
    text = request.POST.get("eft", "")
    try:
        parsed = services.import_eft(text)
    except ValueError as exc:
        return render(request, "fitting/index.html",
                      {"fits": [], "error": str(exc), "brand": _("Tocha's Lab")})
    if not parsed["ship_type_id"]:
        return render(request, "fitting/index.html",
                      {"fits": [], "error": _("Could not recognise the ship hull in that fit."),
                       "brand": _("Tocha's Lab")})
    fit = services.create_fit(request.user, name=parsed["fit_name"],
                              ship_type_id=parsed["ship_type_id"], items=parsed["items"],
                              origin="eft")
    return redirect("fitting:detail", pk=fit.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def import_killmail(request, killmail_id: int):
    from apps.killboard.fitrender import esi_fitting
    from apps.killboard.models import Killmail
    km = get_object_or_404(Killmail, killmail_id=killmail_id)
    ship_type_id, items = services.items_from_esi_fitting(esi_fitting(km))
    fit = services.create_fit(request.user, name=f"Killmail {killmail_id}",
                              ship_type_id=ship_type_id, items=items, origin="killmail")
    return redirect("fitting:detail", pk=fit.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def import_doctrine(request, fit_id: int):
    from apps.doctrines.models import DoctrineFit
    dfit = get_object_or_404(DoctrineFit, pk=fit_id)
    ship_type_id, items = services.items_from_doctrine_fit(dfit)
    fit = services.create_fit(request.user, name=f"{dfit.name} (candidate)",
                              ship_type_id=ship_type_id, items=items, origin="doctrine")
    return redirect("fitting:detail", pk=fit.pk)


# --------------------------------------------------------------------------- #
# Detail / editor
# --------------------------------------------------------------------------- #
@login_required
@feature_required("tochas_lab")
def detail(request, pk):
    fit = get_object_or_404(Fit.objects.select_related("current_revision"), pk=pk)
    if not fit.can_view(request.user):
        raise Http404
    rev = fit.current_revision
    items = _item_display(rev.items if rev else [])
    skills = _skill_profile(request, request.GET.get("skills"))
    op = _op_profile(request)
    telemetry = services.evaluate(fit.ship_type_id, rev.items if rev else [], skills, op)
    context = {
        "fit": fit, "revision": rev, "items": items, "telemetry": telemetry,
        "editable": fit.can_edit(request.user),
        "skill_label": skills.label, "brand": _("Tocha's Lab"),
        "ship_name": services._type_names({fit.ship_type_id}).get(fit.ship_type_id, ""),
        "linked_pilots": pilots.linked_pilots(request.user),
        "can_promote": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        "price": services.price_fit(fit.ship_type_id, rev.items if rev else []),
        "stock": services.stock_coverage(fit.ship_type_id, rev.items if rev else []),
        "missing_skills": _enrich_skills(telemetry.get("missing_skills", [])),
        "show_skills": True,
    }
    if context["can_promote"]:
        from apps.doctrines.models import Doctrine
        context["doctrines"] = list(
            Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).order_by("name")[:200]
        )
    return render(request, "fitting/detail.html", context)


def _enrich_skills(missing: list[dict]) -> list[dict]:
    """Add skill names + a rough training estimate to the engine's missing-skill list."""
    if not missing:
        return []
    from apps.sde.models import SdeType
    from apps.skills.services import SP_PER_HOUR, sp_between_levels
    ids = {int(m["skill_type_id"]) for m in missing}
    meta = {t.type_id: (t.name, t.rank or 1)
            for t in SdeType.objects.filter(type_id__in=ids)}
    out = []
    for m in missing:
        sid = int(m["skill_type_id"])
        name, rank = meta.get(sid, (f"Skill {sid}", 1))
        sp = sp_between_levels(rank, int(m["have_level"]), int(m["required_level"]))
        out.append({**m, "name": name, "seconds": int(sp / SP_PER_HOUR * 3600)})
    return out


@login_required
@feature_required("tochas_lab")
@require_POST
def telemetry(request):
    """Stateless live recompute for the editor. Never persists; server-authoritative.

    Renders the SAME telemetry partial the saved-fit page uses, so the editor and a stored
    render can never disagree and no calculation logic is duplicated in JavaScript."""
    ship_type_id = int(request.POST.get("ship_type_id", 0))
    try:
        items = _parse_items(request.POST.get("items", "[]"))
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "invalid_items"}, status=400)
    skills = _skill_profile(request, request.POST.get("skills"))
    op = _op_profile(request)
    result = services.evaluate(ship_type_id, items, skills, op)
    return render(request, "fitting/_telemetry.html", {
        "telemetry": result, "show_skills": True, "skill_label": skills.label,
        "missing_skills": _enrich_skills(result.get("missing_skills", [])),
    })


@login_required
@feature_required("tochas_lab")
@require_POST
def save(request, pk):
    fit = _require_owner(request, pk)
    try:
        items = _parse_items(request.POST.get("items", "[]"))
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "invalid_items"}, status=400)
    ship_type_id = int(request.POST.get("ship_type_id") or fit.ship_type_id)
    if request.POST.get("name"):
        fit.name = request.POST["name"][:200]
        fit.save(update_fields=["name", "updated_at"])
    services.save_revision(fit, ship_type_id=ship_type_id, items=items, user=request.user,
                           change_summary=request.POST.get("summary", "")[:280])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "fit": fit.pk})
    return redirect("fitting:detail", pk=fit.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def fork(request, pk):
    fit = get_object_or_404(Fit.objects.select_related("current_revision"), pk=pk)
    if not fit.can_view(request.user):
        raise Http404
    rev = fit.current_revision
    if not rev:
        raise Http404
    new = services.fork_fit(fit, rev, request.user)
    return redirect("fitting:detail", pk=new.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def share(request, pk):
    fit = _require_owner(request, pk)
    services.create_share_link(fit, actor=request.user)
    return redirect("fitting:detail", pk=fit.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def unshare(request, pk):
    fit = _require_owner(request, pk)
    services.revoke_share_link(fit, actor=request.user)
    return redirect("fitting:detail", pk=fit.pk)


@login_required
@feature_required("tochas_lab")
@require_POST
def delete(request, pk):
    fit = _require_owner(request, pk)
    fit.is_archived = True
    fit.save(update_fields=["is_archived", "updated_at"])
    audit_log(request.user, "tochaslab.fit.archived", target_type="fitting.Fit",
              target_id=fit.pk, ip=client_ip(request))
    return redirect("fitting:index")


@login_required
@feature_required("tochas_lab")
def export_eft(request, pk):
    fit = get_object_or_404(Fit.objects.select_related("current_revision"), pk=pk)
    if not fit.can_view(request.user):
        raise Http404
    rev = fit.current_revision
    text = services.export_eft(fit.ship_type_id, rev.items if rev else [], fit.name)
    resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="fit-{fit.pk}.txt"'
    return resp


@login_required
@feature_required("tochas_lab")
def training_export(request, pk):
    """The fit's missing skills for the selected pilot as an EVE skill-planner paste."""
    fit = get_object_or_404(Fit.objects.select_related("current_revision"), pk=pk)
    if not fit.can_view(request.user):
        raise Http404
    rev = fit.current_revision
    skills = _skill_profile(request, request.GET.get("skills"))
    telemetry = services.evaluate(fit.ship_type_id, rev.items if rev else [], skills)
    text = services.training_plan_text(telemetry.get("missing_skills", []))
    resp = HttpResponse(text or "# This pilot can already fly this fit.",
                        content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="fit-{fit.pk}-skills.txt"'
    return resp


@login_required
@feature_required("tochas_lab")
def compare(request, pk):
    fit = get_object_or_404(Fit.objects.select_related("current_revision"), pk=pk)
    if not fit.can_view(request.user):
        raise Http404
    revisions = list(fit.revisions.all()[:50])
    rev_b = fit.current_revision
    rev_a_id = request.GET.get("rev")
    rev_a = next((r for r in revisions if str(r.revision_number) == rev_a_id), None) or \
        (revisions[1] if len(revisions) > 1 else rev_b)
    skills = _skill_profile(request, request.GET.get("skills"))
    diff = services.compare(rev_a, rev_b, skills) if (rev_a and rev_b) else None
    return render(request, "fitting/compare.html",
                  {"fit": fit, "revisions": revisions, "rev_a": rev_a, "rev_b": rev_b,
                   "diff": diff, "brand": _("Tocha's Lab")})


@login_required
@feature_required("tochas_lab")
@require_POST
def promote(request, pk):
    if not rbac.has_role(request.user, rbac.ROLE_OFFICER):
        raise Http404
    fit = get_object_or_404(Fit.objects.select_related("current_revision"), pk=pk)
    from apps.doctrines.models import Doctrine
    doctrine = Doctrine.objects.filter(pk=request.POST.get("doctrine")).first()
    if not doctrine or not fit.current_revision:
        return redirect("fitting:detail", pk=fit.pk)
    services.promote_to_doctrine(fit, fit.current_revision, doctrine, request.user)
    return redirect("fitting:detail", pk=fit.pk)


# --------------------------------------------------------------------------- #
# Search + public share
# --------------------------------------------------------------------------- #
@login_required
@feature_required("tochas_lab")
def search_modules(request):
    return JsonResponse({"results": search_types(request.GET.get("q", ""), limit=20)})


@login_required
@feature_required("tochas_lab")
def search_hulls(request):
    return JsonResponse({"results": search_ships(request.GET.get("q", ""), limit=20)})


@feature_required("tochas_lab")
def shared(request, token):
    """Public read-only view resolved ONLY by unguessable token; never by id."""
    fit = Fit.objects.select_related("current_revision").filter(
        share_token=token, share_revoked=False).first()
    if not fit or not fit.public_link_active:
        raise Http404
    rev = fit.current_revision
    skills = services.SkillProfile.omniscient(label=_("All V"))
    telemetry = services.evaluate(fit.ship_type_id, rev.items if rev else [], skills)
    return render(request, "fitting/shared.html", {
        "fit": fit, "items": _item_display(rev.items if rev else []),
        "telemetry": telemetry, "brand": _("Tocha's Lab"),
        "ship_name": services._type_names({fit.ship_type_id}).get(fit.ship_type_id, ""),
    })
