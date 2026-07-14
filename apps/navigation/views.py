"""Navigation tools: a gate route planner and a cyno jump planner (DOTLAN-style).

Public, like the freight calculator — pure routing math over public SDE data, no
corp data is exposed. Origin/destination are solar systems, so no ESI token is
needed. The gate route uses ESI ``POST /route``; the jump route uses the local
cyno proximity graph (apps/logistics/jumps.py).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _t
from django.views.decorators.http import require_POST

from apps.logistics.jumps import (
    JUMP_SHIPS,
    MAX_JUMP_RANGE_LY,
    SHIPS_BY_KEY,
    effective_range,
    systems_in_range,
    systems_within_jumps,
)
from apps.logistics.routing import (
    ROUTE_PREFERENCES,
    RouteUnavailable,
    route_plan_multi,
    security_band,
)
from apps.logistics.ships import SHIP_GROUPS
from apps.sde.models import SdeSolarSystem
from apps.sde.search import search_systems
from core import rbac
from core.rbac import role_required

from .models import AnsiblexBridge, CynoBeacon
from .services import (
    ansiblex_connections,
    incursion_systems,
    resolve_avoidance,
    resolve_waypoints,
)

# Upper bound on parsed system-id lists accepted from the public map query params
# (``hl``/``route``/``sys``); far above any real route, but stops an unbounded list
# becoming one oversized ``WHERE … IN`` query on an unauthenticated endpoint.
_MAX_MAP_IDS = 256


def _avoidance(request: HttpRequest) -> dict:
    """Parse the shared avoidance controls into an ``avoid`` system-id set."""
    sys_text = request.GET.get("avoid_sys", "")
    region_text = request.GET.get("avoid_region", "")
    incursions = request.GET.get("avoid_incursions") == "1"
    avoid, unresolved = resolve_avoidance(sys_text, region_text)
    if incursions:
        avoid |= incursion_systems()
    return {
        "avoid": avoid, "unresolved": unresolved,
        "avoid_sys": sys_text, "avoid_region": region_text, "avoid_incursions": incursions,
    }


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _clamp_range_ly(raw) -> float | None:
    """Parse a user 'range (ly)' override, bounded to a sane jump range. Returns None
    for empty/invalid input. Rejects non-finite / huge values so a hostile ?range=1e400
    can't collapse the O(n) cyno-graph build into O(n²) + unbounded cache (DoS) — the
    planner pages are public/anonymous-reachable."""
    import math

    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return max(0.1, min(val, MAX_JUMP_RANGE_LY))


def _resolve_system(id_value, name_value) -> SdeSolarSystem | None:
    """Resolve a picker submission: the hidden system id, else the typed name."""
    id_value = (id_value or "").strip()
    if id_value.isdigit():
        return SdeSolarSystem.objects.filter(system_id=int(id_value)).first()
    name = (name_value or "").strip()
    if name:
        return (
            SdeSolarSystem.objects.filter(name__iexact=name).first()
            or SdeSolarSystem.objects.filter(name__istartswith=name).order_by("name").first()
        )
    return None


def system_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the system pickers ([{type_id, name}] shape)."""
    return JsonResponse(search_systems(request.GET.get("q", ""), limit=20), safe=False)


def map_index(request: HttpRequest) -> HttpResponse:
    """The whole-cluster universe map + a searchable list of every region."""
    from apps.sde.models import SdeRegion

    from .maps import universe_map

    return render(request, "navigation/map_index.html", {
        "regions": list(SdeRegion.objects.values("region_id", "name")),
        "universe": universe_map(),
    })


def map_region(request: HttpRequest, region: str) -> HttpResponse:
    """An SVG map of one region — systems projected from the SDE, stargate links,
    with switchable security / activity / sovereignty overlays."""
    from apps.sde.models import SdeRegion

    from .maps import OVERLAYS, region_map

    reg = None
    if region.isdigit():
        reg = SdeRegion.objects.filter(region_id=int(region)).first()
    if reg is None:
        reg = SdeRegion.objects.filter(name__iexact=region.replace("_", " ")).first()
    if reg is None:
        return render(request, "navigation/map_region.html", {"missing": region}, status=404)

    overlay = request.GET.get("overlay", "security")
    if overlay not in {k for k, _ in OVERLAYS}:
        overlay = "security"
    hl_param = request.GET.get("hl", "")
    # Cap parsed id-lists on these public map endpoints so an unbounded
    # ``?hl=/?route=/?sys=`` list can't be turned into one giant WHERE … IN query.
    highlight = {int(x) for x in hl_param.split(",")[:_MAX_MAP_IDS] if x.strip().isdigit()}

    # A planned route (ordered system ids) is drawn as a directional path; its systems
    # are also highlighted. Only the legs whose both ends are in this region are drawn.
    route_param = request.GET.get("route", "")
    route_ids = [int(x) for x in route_param.split(",")[:_MAX_MAP_IDS] if x.strip().isdigit()]
    highlight |= set(route_ids)

    rmap = region_map(reg.region_id, overlay=overlay)
    route_segments = []
    if rmap and route_ids:
        pos = {n["id"]: (n["px"], n["py"]) for n in rmap["nodes"]}
        for a, b in zip(route_ids, route_ids[1:], strict=False):
            if a in pos and b in pos:
                (x1, y1), (x2, y2) = pos[a], pos[b]
                route_segments.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    return render(request, "navigation/map_region.html", {
        "map": rmap,
        "overlays": OVERLAYS, "overlay": overlay,
        "highlight": highlight, "hl_param": hl_param, "region_param": region,
        "route_segments": route_segments, "route_param": route_param,
    })


def route_map_view(request: HttpRequest) -> HttpResponse:
    """One combined map of a planned route across every region it crosses, with the
    path drawn over the systems. Driven by ``?sys=`` (ordered system ids)."""
    from .maps import route_map

    ids = [int(x) for x in request.GET.get("sys", "").split(",")[:_MAX_MAP_IDS] if x.strip().isdigit()]
    rmap = route_map(ids) if ids else None
    return render(request, "navigation/route_map.html", {"map": rmap})


def range_map_view(request: HttpRequest) -> HttpResponse:
    """A jump-range reach map — the origin plus every in-range system, with reach
    spokes fanning out. Driven by ``?from=<origin>`` + ``?sys=`` (in-range ids)."""
    from .maps import range_map

    origin_raw = (request.GET.get("from") or "").strip()
    origin_id = int(origin_raw) if origin_raw.isdigit() else None
    ids = [int(x) for x in request.GET.get("sys", "").split(",")[:_MAX_MAP_IDS] if x.strip().isdigit()]
    rmap = range_map(origin_id, ids) if origin_id else None
    return render(request, "navigation/range_map.html", {"map": rmap})


def system_detail(request: HttpRequest, system: str) -> HttpResponse:
    """An at-a-glance dossier for one solar system: security, exits, stations,
    celestials, live activity, sovereignty and the latest kills recorded there."""
    from .map_overlays import (
        system_jumps,
        system_kills,
        system_topology,
    )
    from .maps import _sov_names
    from .system_info import system_facts

    sys_obj = None
    if system.isdigit():
        sys_obj = SdeSolarSystem.objects.filter(system_id=int(system)).first()
    if sys_obj is None:
        sys_obj = SdeSolarSystem.objects.filter(name__iexact=system.replace("_", " ")).first()
    if sys_obj is None:
        return render(request, "navigation/system_detail.html", {"missing": system}, status=404)

    sid = sys_obj.system_id
    facts = system_facts(sid)
    topo = system_topology(sid)

    # Live, public-ESI activity (cached ~1h via the overlay helpers).
    traffic = system_jumps().get(sid, 0)
    kills = system_kills().get(sid, {})
    incursion = sid in incursion_systems()

    sov_holder = None
    try:
        from .map_overlays import sovereignty
        sov = sovereignty().get(sid) or {}
        holder = sov.get("alliance_id") or sov.get("corporation_id") or sov.get("faction_id")
        if holder:
            sov_holder = _sov_names(sovereignty(), [sid]).get(holder) or f"#{holder}"
    except Exception:  # noqa: BLE001,S110 - sovereignty is best-effort live data
        pass

    fw = None
    try:
        from .map_overlays import faction_warfare
        from .maps import FW_FACTIONS
        f = faction_warfare().get(sid)
        if f:
            pct = round(100 * f["vp"] / f["threshold"]) if f["threshold"] else 0
            fw = {"faction": FW_FACTIONS.get(f["occupier"], ("Unaligned", ""))[0],
                  "contested": f["contested"], "pct": pct}
    except Exception:  # noqa: BLE001,S110 - FW is best-effort live data
        pass

    # Named NPC stations also benefit from the cached topology station ids, but the
    # SDE already names them, so facts["stations"] is the source of truth here.
    return render(request, "navigation/system_detail.html", {
        "facts": facts,
        "topo": topo,
        "live": {
            "traffic": traffic,
            "ship_kills": kills.get("ship_kills", 0),
            "pod_kills": kills.get("pod_kills", 0),
            "npc_kills": kills.get("npc_kills", 0),
            "incursion": incursion,
            "sov": sov_holder,
            "fw": fw,
        },
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def roaming(request: HttpRequest) -> HttpResponse:
    """Intel: rank systems by NPC kills (ratting/mining activity) to suggest where a
    roaming gang will find targets — biased toward busy-but-undefended space."""
    from .roaming import roaming_targets

    region_q = (request.GET.get("region") or "").strip()
    band = request.GET.get("band", "nullsec")
    if band not in {"all", "nullsec", "lowsec", "highsec"}:
        band = "nullsec"
    region_obj = None
    if region_q:
        from apps.sde.models import SdeRegion
        if region_q.isdigit():
            region_obj = SdeRegion.objects.filter(region_id=int(region_q)).first()
        if region_obj is None:
            region_obj = SdeRegion.objects.filter(name__iexact=region_q).first()

    targets = roaming_targets(
        region_id=region_obj.region_id if region_obj else None, band=band, limit=40
    )
    from apps.sde.models import SdeRegion
    return render(request, "navigation/roaming.html", {
        "targets": targets,
        "band": band, "region_q": region_q, "region_obj": region_obj,
        "regions": list(SdeRegion.objects.values("region_id", "name")),
        "count": len(targets),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def gatecamp(request: HttpRequest) -> HttpResponse:
    """Intel: systems showing gate-camp signatures right now (recent pods/ships,
    chokepoints), so pilots know which hops to scout or avoid."""
    from .gatecamp import camp_watch

    band = request.GET.get("band", "all")
    if band not in {"all", "nullsec", "lowsec", "highsec"}:
        band = "all"
    region_q = (request.GET.get("region") or "").strip()
    region_obj = None
    if region_q:
        from apps.sde.models import SdeRegion
        if region_q.isdigit():
            region_obj = SdeRegion.objects.filter(region_id=int(region_q)).first()
        if region_obj is None:
            region_obj = SdeRegion.objects.filter(name__iexact=region_q).first()

    rows = camp_watch(
        region_id=region_obj.region_id if region_obj else None, band=band, limit=40
    )
    from apps.sde.models import SdeRegion
    return render(request, "navigation/gatecamp.html", {
        "rows": rows, "band": band, "region_q": region_q, "region_obj": region_obj,
        "regions": list(SdeRegion.objects.values("region_id", "name")),
        "count": len(rows),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def beacons(request: HttpRequest) -> HttpResponse:
    """The corp's Ansiblex jump-bridge network — viewable by members, edited by officers."""
    return render(request, "navigation/beacons.html", {
        "bridges": AnsiblexBridge.objects.all(),
        "cyno_beacons": CynoBeacon.objects.filter(active=True),
        "can_manage": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def beacon_sync(request: HttpRequest) -> HttpResponse:
    """Import the Ansiblex + cyno-beacon network from ESI (Director token)."""
    from .esi_sync import sync_jump_network

    result = sync_jump_network()
    if result["status"] == "ok":
        messages.success(request, result["message"])
    elif result["status"] == "no_scope":
        messages.warning(request, result["message"] + " " + _t("Grant it on the ESI Scopes page."))
    else:
        messages.error(request, result["message"])
    return redirect("navigation:beacons")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def beacon_add(request: HttpRequest) -> HttpResponse:
    frm = _resolve_system(request.POST.get("from"), request.POST.get("from_q"))
    to = _resolve_system(request.POST.get("to"), request.POST.get("to_q"))
    if not frm or not to or frm.system_id == to.system_id:
        messages.error(request, _t("Pick two different systems for the bridge."))
        return redirect("navigation:beacons")
    # Store canonically (lower system id first); the route helper adds both directions.
    a, b = sorted([(frm.system_id, frm.name), (to.system_id, to.name)])
    name = (request.POST.get("name") or "").strip()
    note = (request.POST.get("note") or "").strip()
    obj, created = AnsiblexBridge.objects.get_or_create(
        from_system_id=a[0], to_system_id=b[0],
        defaults={"from_system_name": a[1], "to_system_name": b[1], "name": name, "note": note},
    )
    if not created:
        obj.active = True
        obj.from_system_name, obj.to_system_name = a[1], b[1]
        obj.name, obj.note = name or obj.name, note or obj.note
        obj.save()
    messages.success(request, _t("Jump bridge %(a)s ⇄ %(b)s saved.") % {"a": a[1], "b": b[1]})
    return redirect("navigation:beacons")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def beacon_remove(request: HttpRequest, pk: int) -> HttpResponse:
    get_object_or_404(AnsiblexBridge, pk=pk).delete()
    messages.success(request, _t("Jump bridge removed."))
    return redirect("navigation:beacons")


def route_planner(request: HttpRequest) -> HttpResponse:
    """Gate route planner: shortest / safer / less-secure, system by system."""
    origin = _resolve_system(request.GET.get("from"), request.GET.get("from_q"))
    dest = _resolve_system(request.GET.get("to"), request.GET.get("to_q"))
    preference = request.GET.get("pref", "safer")
    if preference not in ROUTE_PREFERENCES:
        preference = "safer"

    av = _avoidance(request)
    waypoints, wp_unresolved = resolve_waypoints(request.GET.get("waypoints", ""))
    use_ansiblex = request.GET.get("ansiblex") == "1"
    ansiblex_count = AnsiblexBridge.objects.filter(active=True).count()
    connections = ansiblex_connections() if use_ansiblex else None
    result = error = None
    if origin and dest:
        seq = [origin.system_id, *[w.system_id for w in waypoints], dest.system_id]
        try:
            result = route_plan_multi(seq, preference, avoid=av["avoid"], connections=connections)
        except RouteUnavailable as exc:
            error = str(exc)
    elif request.GET.get("from_q") or request.GET.get("to_q"):
        error = _t("Pick both systems from the list.")

    map_link = camp = None
    if result and dest:
        seq_ids = ",".join(str(s["system_id"]) for s in result["systems"])
        map_link = f"{reverse('navigation:route_map')}?sys={seq_ids}"
        # Gate-camp risk on each hop (public ESI) — annotate systems + a banner.
        from .gatecamp import route_camp_check
        camp = route_camp_check([s["system_id"] for s in result["systems"]])
        for s in result["systems"]:
            s["camp"] = camp["by_id"].get(s["system_id"])
    return render(request, "navigation/route_planner.html", {
        "camp": camp,
        "map_link": map_link,
        "origin": origin, "dest": dest, "preference": preference,
        "preferences": [(k, label) for k, (_, label) in ROUTE_PREFERENCES.items()],
        "waypoints_text": request.GET.get("waypoints", ""),
        "waypoints": waypoints, "wp_unresolved": wp_unresolved,
        "use_ansiblex": use_ansiblex, "ansiblex_count": ansiblex_count,
        "result": result, "error": error, **av,
    })


def jump_planner(request: HttpRequest) -> HttpResponse:
    """Cyno jump planner: pick a hull + skills, get a jump/gate/mixed-mode route.

    Delegates the routing decision and fuel maths to
    :func:`apps.navigation.jump_service.plan_jump`, which resolves the route mode
    from the hull's capabilities and, for a high-sec destination a jump freighter
    can't cyno into, plans jump legs to a low-sec exit plus a real stargate route.
    """
    from apps.logistics.ships import DEFAULT_SHIP_KEY, profile_for

    from .jump_service import jump_skills_for_user, plan_jump
    from .models import JumpPlannerConfig, SavedJumpRoute

    cfg = JumpPlannerConfig.active()
    profile = profile_for(request.GET.get("ship", DEFAULT_SHIP_KEY))

    origin = _resolve_system(request.GET.get("from"), request.GET.get("from_q"))
    dest = _resolve_system(request.GET.get("to"), request.GET.get("to_q"))
    # 4.1: auto-fill the skill inputs from the pilot's own synced skills (least-privilege
    # reuse — no new scope) so the default reflects their REAL range/fuel, not leadership's
    # generic defaults. An explicit ?jdc=… still wins, so the pilot can override any value.
    pilot_skills = jump_skills_for_user(request.user)
    d_jdc = pilot_skills["jdc"] if pilot_skills else cfg.default_jdc
    d_jfc = pilot_skills["jfc"] if pilot_skills else cfg.default_jfc
    d_jf = pilot_skills["jf"] if pilot_skills else cfg.default_jf_skill
    jdc = _clamp(request.GET.get("jdc", d_jdc), 0, 5, cfg.default_jdc)
    jfc = _clamp(request.GET.get("jfc", d_jfc), 0, 5, cfg.default_jfc)
    jf_skill = _clamp(request.GET.get("jf", d_jf), 0, 5, cfg.default_jf_skill)
    jde_rigs = _clamp(request.GET.get("rigs", 0), 0, 3, 0) if profile.rig_eligible else 0

    range_ly = effective_range(profile.base_range_ly, jdc)
    custom = (request.GET.get("range") or "").strip()
    custom_range = _clamp_range_ly(custom) if custom else None
    if custom_range is not None:
        range_ly = custom_range

    preference = request.GET.get("pref", cfg.default_preference)
    if preference not in ROUTE_PREFERENCES:
        preference = cfg.default_preference if cfg.default_preference in ROUTE_PREFERENCES else "safer"

    av = _avoidance(request)
    # Merge leadership's corp avoid-lists into the user's.
    corp_avoid, _ = resolve_avoidance(cfg.avoid_systems, cfg.avoid_regions)
    avoid = av["avoid"] | corp_avoid

    require_stations = request.GET.get("stations_only") == "1"
    waypoints, wp_unresolved = resolve_waypoints(request.GET.get("waypoints", ""))
    use_ansiblex = request.GET.get("ansiblex") == "1"
    connections = ansiblex_connections() if use_ansiblex else None
    exit_override = None
    if cfg.allow_pilot_exit_override:
        raw_exit = (request.GET.get("exit") or "").strip()
        exit_override = int(raw_exit) if raw_exit.isdigit() else None

    result = error = None
    if not cfg.enabled:
        error = _t("The jump planner is currently disabled by leadership.")
    elif origin and dest:
        plan = plan_jump(
            origin, dest, profile, jdc=jdc, jfc=jfc, jf_skill=jf_skill, jde_rigs=jde_rigs,
            custom_range=custom_range, waypoints=waypoints, preference=preference,
            avoid=avoid, require_stations=require_stations, prefer_stations=cfg.prefer_stations,
            connections=connections, safety_margin_pct=cfg.fuel_safety_margin_pct,
            exit_system_id=exit_override,
        )
        if plan["can_plan"]:
            result = plan
        else:
            error = plan["error"]
    elif request.GET.get("from_q") or request.GET.get("to_q"):
        error = _t("Pick both systems from the list.")

    map_link = None
    if result and result.get("map_ids"):
        seq_ids = ",".join(str(s) for s in result["map_ids"])
        map_link = f"{reverse('navigation:route_map')}?sys={seq_ids}"

    saved_routes = None
    if request.user.is_authenticated and cfg.allow_saved_routes:
        saved_routes = list(SavedJumpRoute.objects.filter(owner=request.user)[:12])

    return render(request, "navigation/jump_planner.html", {
        "map_link": map_link,
        "origin": origin, "dest": dest, "ship_groups": SHIP_GROUPS, "profile": profile,
        "jdc": jdc, "jfc": jfc, "jf_skill": jf_skill, "jde_rigs": jde_rigs,
        "pilot_skills": pilot_skills,
        "levels": list(range(6)), "rig_levels": list(range(4)),
        "range_ly": round(range_ly, 2), "custom_range": custom if custom_range else "",
        "preference": preference,
        "preferences": [(k, label) for k, (_, label) in ROUTE_PREFERENCES.items()],
        "require_stations": require_stations, "waypoints_text": request.GET.get("waypoints", ""),
        "waypoints": waypoints, "wp_unresolved": wp_unresolved,
        "use_ansiblex": use_ansiblex,
        "ansiblex_count": AnsiblexBridge.objects.filter(active=True).count(),
        "result": result, "error": error, "config": cfg, "saved_routes": saved_routes,
        **av,
    })


def range_finder(request: HttpRequest) -> HttpResponse:
    """Every cyno-capable system within one jump of a chosen system, for a ship."""
    origin = _resolve_system(request.GET.get("from"), request.GET.get("from_q"))
    ship = SHIPS_BY_KEY.get(request.GET.get("ship", "jf"), SHIPS_BY_KEY["jf"])
    jdc = _clamp(request.GET.get("jdc", 5), 0, 5, 5)
    # 4.10: how many cyno jumps to chain out from the origin (1 = the classic reach map).
    max_jumps = _clamp(request.GET.get("jumps", 1), 1, 5, 1)
    range_ly = effective_range(ship["range"], jdc)
    custom = (request.GET.get("range") or "").strip()
    clamped_range = _clamp_range_ly(custom) if custom else None
    custom_used = clamped_range is not None
    if custom_used:
        range_ly = clamped_range

    av = _avoidance(request)
    require_stations = request.GET.get("stations_only") == "1"
    rows = error = None
    if origin:
        if max_jumps > 1:
            found = systems_within_jumps(origin.system_id, range_ly, max_jumps,
                                         avoid=av["avoid"], require_stations=require_stations)
        else:
            found = systems_in_range(origin.system_id, range_ly, avoid=av["avoid"],
                                     require_stations=require_stations)
        if found is None:
            error = _t("That system has no coordinates.")
        else:
            meta = {
                sid: (name, sec, rid)
                for sid, name, sec, rid in SdeSolarSystem.objects.filter(
                    system_id__in=[r["system_id"] for r in found]
                ).values_list("system_id", "name", "security", "region_id")
            }
            from apps.sde.models import SdeRegion
            region_names = dict(
                SdeRegion.objects.filter(region_id__in={m[2] for m in meta.values()})
                .values_list("region_id", "name")
            )
            rows = []
            for r in found:
                name, sec, rid = meta.get(r["system_id"], (f"System {r['system_id']}", -1.0, None))
                rows.append({**r, "name": name, "security": round(sec, 1),
                             "band": security_band(sec), "region": region_names.get(rid, "")})
    elif request.GET.get("from_q"):
        error = _t("Pick a system from the list.")

    map_link = None
    if rows and origin:
        seq_ids = ",".join(str(r["system_id"]) for r in rows)
        map_link = f"{reverse('navigation:range_map')}?from={origin.system_id}&sys={seq_ids}"
    return render(request, "navigation/range_finder.html", {
        "map_link": map_link,
        "origin": origin, "ships": JUMP_SHIPS, "ship_groups": SHIP_GROUPS, "ship": ship, "jdc": jdc,
        "levels": list(range(6)), "range_ly": round(range_ly, 2),
        "max_jumps": max_jumps, "jump_levels": list(range(1, 6)),
        "custom_range": custom if custom_used else "", "require_stations": require_stations,
        "rows": rows, "count": len(rows) if rows else 0, "error": error, **av,
    })


# --- Saved jump routes ------------------------------------------------------
_SAVED_PARAMS = (
    "ship", "jdc", "jfc", "jf", "rigs", "range", "pref", "waypoints",
    "avoid_sys", "avoid_region", "stations_only", "exit",
)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def jump_route_save(request: HttpRequest) -> HttpResponse:
    """Save the current planner query as a named route (optionally shared with leadership)."""
    from .models import JumpPlannerConfig, SavedJumpRoute

    if not JumpPlannerConfig.active().allow_saved_routes:
        messages.error(request, _t("Saved routes are turned off."))
        return redirect("navigation:jump_planner")

    origin = _resolve_system(request.POST.get("from"), request.POST.get("from_q"))
    dest = _resolve_system(request.POST.get("to"), request.POST.get("to_q"))
    if not origin or not dest:
        messages.error(request, _t("Plan a route first, then save it."))
        return redirect("navigation:jump_planner")

    name = (request.POST.get("name") or f"{origin.name} → {dest.name}").strip()[:120]
    visibility = request.POST.get("visibility")
    if visibility not in dict(SavedJumpRoute.Visibility.choices):
        visibility = SavedJumpRoute.Visibility.PRIVATE
    raw_exit = (request.POST.get("exit") or "").strip()
    SavedJumpRoute.objects.create(
        owner=request.user, name=name,
        origin_system_id=origin.system_id, origin_name=origin.name,
        dest_system_id=dest.system_id, dest_name=dest.name,
        ship_key=(request.POST.get("ship") or "rhea")[:24],
        jdc=_clamp(request.POST.get("jdc", 5), 0, 5, 5),
        jfc=_clamp(request.POST.get("jfc", 5), 0, 5, 5),
        jf_skill=_clamp(request.POST.get("jf", 5), 0, 5, 5),
        jde_rigs=_clamp(request.POST.get("rigs", 0), 0, 3, 0),
        preference=(request.POST.get("pref") or "safer")[:12],
        custom_range=_float_or_none(request.POST.get("range")),
        waypoints=(request.POST.get("waypoints") or "")[:300],
        avoid_systems=(request.POST.get("avoid_sys") or "")[:300],
        avoid_regions=(request.POST.get("avoid_region") or "")[:300],
        require_stations=request.POST.get("stations_only") == "1",
        exit_system_id=int(raw_exit) if raw_exit.isdigit() else None,
        visibility=visibility,
        note=(request.POST.get("note") or "").strip()[:200],
    )
    messages.success(request, _t("Saved route “%(name)s”.") % {"name": name})
    return redirect("navigation:jump_routes")


@login_required
@role_required(rbac.ROLE_MEMBER)
def jump_routes(request: HttpRequest) -> HttpResponse:
    """My saved routes, plus (for officers) routes shared with leadership."""
    from .models import SavedJumpRoute

    mine = list(SavedJumpRoute.objects.filter(owner=request.user))
    shared = None
    if rbac.has_role(request.user, rbac.ROLE_OFFICER):
        shared = list(
            SavedJumpRoute.objects.filter(visibility=SavedJumpRoute.Visibility.LEADERSHIP)
            .exclude(owner=request.user).select_related("owner")
        )
    return render(request, "navigation/jump_routes.html", {"mine": mine, "shared": shared})


@login_required
@role_required(rbac.ROLE_MEMBER)
def jump_route_open(request: HttpRequest, pk: int) -> HttpResponse:
    """Reopen a saved route: rebuild the planner query string and redirect."""
    from urllib.parse import urlencode

    from .models import SavedJumpRoute

    route = get_object_or_404(SavedJumpRoute, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if route.owner_id != request.user.id and not (
        is_officer and route.visibility == SavedJumpRoute.Visibility.LEADERSHIP
    ):
        messages.error(request, _t("You can't open that route."))
        return redirect("navigation:jump_routes")
    params = {
        "from": route.origin_system_id, "from_q": route.origin_name,
        "to": route.dest_system_id, "to_q": route.dest_name,
        "ship": route.ship_key, "jdc": route.jdc, "jfc": route.jfc, "jf": route.jf_skill,
        "rigs": route.jde_rigs, "pref": route.preference,
        "waypoints": route.waypoints, "avoid_sys": route.avoid_systems,
        "avoid_region": route.avoid_regions,
    }
    if route.custom_range:
        params["range"] = route.custom_range
    if route.require_stations:
        params["stations_only"] = "1"
    if route.exit_system_id:
        params["exit"] = route.exit_system_id
    return redirect(f"{reverse('navigation:jump_planner')}?{urlencode(params)}")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def jump_route_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete a saved route (owner, or an officer for a leadership-shared one)."""
    from .models import SavedJumpRoute

    route = get_object_or_404(SavedJumpRoute, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if route.owner_id == request.user.id or (
        is_officer and route.visibility == SavedJumpRoute.Visibility.LEADERSHIP
    ):
        route.delete()
        messages.success(request, _t("Route removed."))
    else:
        messages.error(request, _t("You can't remove that route."))
    return redirect("navigation:jump_routes")


_MAX_WATCHED_ROUTES = 20  # per-user cap so an armed-route fan-out can't storm the sweep (4.5)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def jump_route_watch(request: HttpRequest, pk: int) -> HttpResponse:
    """Owner toggles camp/incursion watch on their own saved route (4.5). Owner-only —
    the alert DMs them, so only they arm it (no officer override)."""
    from .models import SavedJumpRoute

    route = get_object_or_404(SavedJumpRoute, pk=pk, owner=request.user)
    if not route.watch_enabled:  # about to arm — cap the per-user watch fan-out
        watched = SavedJumpRoute.objects.filter(owner=request.user, watch_enabled=True).count()
        if watched >= _MAX_WATCHED_ROUTES:
            messages.error(request, _t("You can watch at most %(n)d routes at once — turn one off first.")
                           % {"n": _MAX_WATCHED_ROUTES})
            return redirect("navigation:jump_routes")
    route.watch_enabled = not route.watch_enabled
    route.alerted_sig = ""  # reset the tripwire marker so re-arming re-evaluates from scratch
    route.save(update_fields=["watch_enabled", "alerted_sig", "updated_at"])
    if route.watch_enabled:
        messages.success(
            request,
            _t("Camp/incursion watch on for “%(name)s”. You'll be DMed when a threat appears on it.")
            % {"name": route.name},
        )
    else:
        messages.success(
            request,
            _t("Camp/incursion watch off for “%(name)s”.") % {"name": route.name},
        )
    return redirect("navigation:jump_routes")


def _float_or_none(value):
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
