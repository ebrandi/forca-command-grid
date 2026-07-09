"""Per-pilot readiness scoring + quest-log recommendations (Phase 4 + breadth, doc 05 §4).

A light per-pilot pipeline that turns the corp's doctrines, mandatory ships and
strategic-role targets into a six-facet personal score and a ranked, motivating quest
log. It reuses the existing per-pilot helpers (``readiness_summary_for_character``,
``closest_doctrines``, ``qualified_count``) and the personal-asset mirror rather than
re-deriving anything. Recommendations are upserted by ``(user, category, ref_type,
ref_id)`` so a pilot's done/dismissed/snoozed state survives regeneration; an open
recommendation whose gap has closed is dropped.

Facets:
- doctrine     — share of known corp doctrines this pilot can fly
- combat       — PvP recency (killboard)
- logistics    — share of owned mandatory hulls that are at the staging system
- strategic    — share of the corp's skill-detected strategic roles this pilot can fill
- activity     — fleet attendance + recent contributions
- contribution — breadth of recent contributions

A facet stays ``None`` ("no data") only when its source genuinely doesn't exist for
this pilot (e.g. no mandatory ships configured ⇒ logistics is unknown, never zero).
Industry and asset-fitting recommendations (the manufacturing/refit categories) await
a per-pilot build-capability and fit-completeness signal and are a documented follow-up.
"""
from __future__ import annotations

import datetime as dt

# Held LONGER than the warm_pilots beat cadence (every 30 min) so a warmed entry never
# expires into a cold window — pilots read warm cache, not a 1–4 s in-request recompute.
_CACHE_TTL = 5400  # 90 min
_MAX_RECOS = 12     # the quest log shows the strongest dozen, ranked by priority


def cache_key(character_id) -> str:
    return f"readiness:pilot:{character_id}"


def _humanize_eta(seconds: int) -> str:
    days = seconds // 86400
    if days >= 1:
        return f"{days}d"
    hours = seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    return "under an hour"


FACET_KEYS = ("doctrine", "combat", "logistics", "strategic", "activity", "contribution")


# --- shared per-pilot data ---------------------------------------------------
def _skill_snapshot(character):
    from apps.characters.models import CharacterSkillSnapshot

    return (CharacterSkillSnapshot.objects
            .filter(is_latest=True, character_id=character.character_id).first())


def _meets_skills(snap, skills: dict) -> bool:
    if snap is None or not skills:
        return False
    return all(snap.trained_level(int(k)) >= int(v) for k, v in skills.items())


def _mandatory_hulls():
    """Active corp-wide hull-based mandatory ships (role-specific / fit entries excluded)."""
    from apps.readiness.models import MandatoryShip

    return [
        s for s in MandatoryShip.objects.filter(active=True, ship_type_id__isnull=False)
        if not s.applies_to_role
    ]


def _owned_hulls(character) -> dict:
    """``type_id → list[Asset]`` the pilot owns (one row per location)."""
    from collections import defaultdict

    from apps.stockpile.models import Asset

    owned: dict[int, list] = defaultdict(list)
    for a in (Asset.objects
              .filter(owner_type=Asset.Owner.CHARACTER, owner_id=character.character_id)
              .select_related("location")):
        owned[a.type_id].append(a)
    return owned


# --- facets ------------------------------------------------------------------
def _base_facets(character, user) -> tuple[dict, list]:
    """doctrine / combat / activity / contribution — the account-and-killboard facets."""
    from django.utils import timezone

    from apps.doctrines.services import readiness_summary_for_character

    now = timezone.now()
    since_30 = now - dt.timedelta(days=30)
    since_90 = now - dt.timedelta(days=90)
    facets: dict[str, int | None] = dict.fromkeys(FACET_KEYS, None)

    # doctrine — personal coverage of the corp's doctrines (honest: unknown excluded).
    summary = readiness_summary_for_character(character)
    known = [r for r in summary if r["status"] != "unknown"]
    ready = [r for r in known if r["status"] in ("viable", "optimal")]
    if known:
        facets["doctrine"] = round(100 * len(ready) / len(known))

    # combat — PvP recency from the killboard.
    from apps.killboard.models import KillmailParticipant

    parts = KillmailParticipant.objects.filter(character_id=character.character_id)
    if parts.exists():
        if parts.filter(killmail__killmail_time__gte=since_30).exists():
            facets["combat"] = 100
        elif parts.filter(killmail__killmail_time__gte=since_90).exists():
            facets["combat"] = 60
        else:
            facets["combat"] = 30

    # activity + contribution — fleet attendance and recent contribution events.
    contrib_events = []
    if user is not None:
        from apps.operations.models import OperationAttendance
        from apps.pilots.models import ContributionEvent

        att_30 = OperationAttendance.objects.filter(user=user, created_at__gte=since_30).count()
        contrib_events = list(
            ContributionEvent.objects.filter(user=user, occurred_at__gte=since_30)
        )
        facets["activity"] = min(100, att_30 * 30 + min(len(contrib_events), 5) * 10)
        facets["contribution"] = min(100, len(contrib_events) * 20)

    return facets, contrib_events


def _strategic(character, snap) -> tuple[int | None, list[dict]]:
    """``(facet, role recos)`` — strategic value = share of skill-roles this pilot can fill.

    Emits a "volunteer" reco for each *scarce* role (corp short of its target) the pilot
    already qualifies for — turning a corp shortage into a personal opportunity.
    """
    from apps.readiness.dimensions.roles import qualified_count
    from apps.readiness.models import StrategicRoleTarget

    targets = [
        t for t in StrategicRoleTarget.objects.filter(
            active=True, detection=StrategicRoleTarget.Detection.SKILLS)
        if t.desired_count and (t.detection_params or {}).get("skills")
    ]
    if not targets:
        return None, []  # no skill-detected roles configured ⇒ unknown, never zero

    qualifies = {
        t.role_key: _meets_skills(snap, (t.detection_params or {}).get("skills") or {})
        for t in targets
    }
    facet = round(100 * sum(1 for ok in qualifies.values() if ok) / len(targets))

    recos = []
    for i, t in enumerate(targets):
        if not qualifies[t.role_key]:
            continue
        qc = qualified_count(t)
        if qc is None or qc >= t.desired_count:
            continue  # role already staffed — no shortage to fill
        recos.append({
            "category": "role", "ref_type": "role", "ref_id": t.role_key,
            "title": f"Volunteer as {t.label}",
            "detail": (f"The corp is short on {t.label} ({qc}/{t.desired_count}) and you already "
                       "have the skills. Flying this role directly raises corp readiness."),
            "priority": 95 - i, "points": 10, "action_url": "/operations/",
        })
    return facet, recos


def _ship_logistics(character) -> tuple[int | None, list[dict], list[dict]]:
    """``(logistics facet, ship recos, logistics recos)`` from mandatory hulls + assets.

    ship reco  — a mandatory hull the pilot doesn't own enough of.
    logi reco  — an owned mandatory hull that isn't at its required staging system.
    facet      — share of owned, location-bound mandatory hulls that ARE at staging.
    """
    ships = _mandatory_hulls()
    if not ships:
        return None, [], []
    owned = _owned_hulls(character)
    ship_recos: list[dict] = []
    logi_recos: list[dict] = []
    owned_located = 0
    at_staging = 0
    for i, s in enumerate(ships):
        rows = owned.get(s.ship_type_id, [])
        total_qty = sum(r.quantity for r in rows)
        if total_qty < (s.required_quantity or 1):
            ship_recos.append({
                "category": "ship", "ref_type": "mandatory_ship", "ref_id": str(s.id),
                "title": f"Get your {s.label}",
                "detail": (f"Every pilot should own {s.required_quantity}× {s.label}. "
                           "Owning your mandatory hull keeps you ready to undock with the fleet."),
                "priority": 90 - i, "points": 8, "action_url": "/store/",
            })
            continue
        if s.required_system_id:
            owned_located += 1
            here = any(r.location and r.location.system_id == s.required_system_id for r in rows)
            if here:
                at_staging += 1
            else:
                logi_recos.append({
                    "category": "logistics", "ref_type": "mandatory_ship", "ref_id": str(s.id),
                    "title": f"Move your {s.label} to staging",
                    "detail": (f"Your {s.label} isn't at the staging system. Bringing it home means "
                               "you can form up without a long haul first."),
                    "priority": 70 - i, "points": 5, "action_url": "/freight/",
                })
    logistics_facet = round(100 * at_staging / owned_located) if owned_located else None
    return logistics_facet, ship_recos, logi_recos


# --- recommendations ---------------------------------------------------------
def _skill_recommendations(character) -> list[dict]:
    """The doctrines this pilot is closest to flying — the training quests."""
    from apps.skills.services import closest_doctrines

    recos = []
    for i, d in enumerate(closest_doctrines(character, limit=5)):
        recos.append({
            "category": "skill", "ref_type": "doctrine", "ref_id": str(d["doctrine_id"]),
            "title": f"Train into {d['doctrine']}",
            "detail": (
                f"You're about {_humanize_eta(d['seconds'])} of training from flying "
                f"{d['doctrine']} — one of the corp's doctrines. Closing this makes you "
                "and the corp more ready."
            ),
            "priority": 100 - i * 10,
            "points": max(2, 12 - i * 2),
            "action_url": "/skills/",
        })
    return recos


def _industry(snap) -> list[dict]:
    """Industry recos: doctrine hulls below the corp's minimum stock that this pilot
    has the *manufacturing* skills to build (Gap C / Toren persona). Honest: a hull is
    only recommended when its blueprint manufacturing-skill data is imported and met."""
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.industry.capability import can_manufacture
    from apps.sde.models import SdeType
    from apps.stockpile.services import shortfalls_against_targets

    hull_ids = set(
        DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE)
        .values_list("ship_type_id", flat=True)
    )
    if not hull_ids:
        return []
    short = {s["type_id"]: s for s in shortfalls_against_targets() if s["type_id"] in hull_ids}
    if not short:
        return []
    names = dict(SdeType.objects.filter(type_id__in=short).values_list("type_id", "name"))
    recos = []
    for i, (tid, s) in enumerate(short.items()):
        if can_manufacture(snap, tid) is True:
            name = names.get(tid, f"Type {tid}")
            recos.append({
                "category": "industry", "ref_type": "type", "ref_id": str(tid),
                "title": f"Build {name} — corp is {s['deficit']} short",
                "detail": (f"You already have the manufacturing skills to build {name}, and the corp "
                           f"stockpile is {s['deficit']} below target. Building a few directly helps the corp."),
                "priority": 65 - i, "points": 8, "action_url": "/industry/",
            })
    return recos


def _asset_fit(character) -> list[dict]:
    """Asset/fit recos (Gap C2): a doctrine hull the pilot owns but hasn't fitted to a
    doctrine — unfitted, or fitted but missing doctrine modules. Uses the per-hull fitted
    state captured from ESI assets (CharacterFittedShip) vs the doctrine fit's modules."""
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.sde.models import SdeType

    fits_by_hull: dict[int, list] = {}
    for fit in DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE):
        required = {m.get("type_id") for m in (fit.modules or []) if m.get("type_id")}
        if required:
            fits_by_hull.setdefault(fit.ship_type_id, []).append((fit.name, required))
    if not fits_by_hull:
        return []

    owned = _owned_hulls(character)  # type_id -> [Asset]
    fitted: dict[int, list] = {}
    for fs in character.fitted_ships.filter(is_latest=True):
        fitted.setdefault(fs.ship_type_id, []).append({int(k) for k in (fs.modules or {})})

    targets = [h for h in fits_by_hull if h in owned]
    if not targets:
        return []
    names = dict(SdeType.objects.filter(type_id__in=targets).values_list("type_id", "name"))
    recos = []
    for i, hull_id in enumerate(targets):
        fit_list = fits_by_hull[hull_id]
        my_fits = fitted.get(hull_id, [])
        if any(required <= mods for (_, required) in fit_list for mods in my_fits):
            continue  # an owned instance already matches a doctrine fit
        name = names.get(hull_id, f"hull {hull_id}")
        if my_fits:
            best = max(my_fits, key=len)
            fit_name = min(fit_list, key=lambda fl: len(fl[1] - best))[0]
            title = f"Finish your {name} fit"
            detail = (f"Your {name} is missing modules for the {fit_name} doctrine fit — "
                      "top it off so it's fleet-ready, not a half-built hull.")
        else:
            title = f"Fit your {name}"
            detail = (f"You own a {name} but it isn't fitted to a doctrine. Fit it (copy a "
                      "doctrine fit from the Doctrines page) so you can undock ready.")
        recos.append({
            "category": "asset", "ref_type": "type", "ref_id": str(hull_id),
            "title": title, "detail": detail,
            "priority": 78 - i, "points": 6, "action_url": "/doctrines/",
        })
    return recos


def _stay_current_reco() -> dict:
    return {
        "category": "role", "ref_type": "activity", "ref_id": "join_op",
        "title": "Fly a fleet this week",
        "detail": "You're on top of your readiness — keep your edge by joining an op.",
        "priority": 40, "points": 4, "action_url": "/operations/",
    }


def _contribution_summary(events) -> dict:
    from collections import Counter

    return dict(Counter(e.kind for e in events))


def compute_pilot(character, *, persist: bool = True) -> dict:
    """Score one pilot and (optionally) persist the snapshot + upsert their quest log."""
    from django.core.cache import cache

    user = getattr(character, "user", None)
    snap = _skill_snapshot(character)

    facets, contrib_events = _base_facets(character, user)
    strategic_facet, role_recos = _strategic(character, snap)
    logistics_facet, ship_recos, logi_recos = _ship_logistics(character)
    facets["strategic"] = strategic_facet
    facets["logistics"] = logistics_facet

    recos = [*_skill_recommendations(character), *ship_recos, *role_recos, *logi_recos,
             *_industry(snap), *_asset_fit(character)]
    if not recos:
        recos = [_stay_current_reco()]
    # Rank by priority and keep the strongest dozen so the quest log stays focused.
    recos = sorted(recos, key=lambda r: r["priority"], reverse=True)[:_MAX_RECOS]

    available = [v for v in facets.values() if v is not None]
    overall = round(sum(available) / len(available)) if available else 0

    if persist and user is not None:
        _persist(user, character, overall, facets, recos)

    payload = {
        "facets": facets,
        "overall": overall,
        "recommendations": recos,
        "contributions": _contribution_summary(contrib_events),
        # 14-day trajectory + week delta, computed here (after persist, so the
        # newest snapshot is included) rather than per page view. Scoped to the
        # CURRENT owner so a transferred character never leaks its previous
        # owner's history.
        "trend": _score_trend(character.character_id, user),
    }
    payload["week_delta"] = (
        overall - payload["trend"][max(0, len(payload["trend"]) - 8)]
        if len(payload["trend"]) >= 2 else None
    )
    cache.set(cache_key(character.character_id), payload, _CACHE_TTL)
    return payload


def _score_trend(character_id, user, days: int = 14) -> list[int]:
    """Daily averages of the pilot's persisted overall scores, oldest first."""
    from datetime import timedelta

    from django.db.models import Avg
    from django.db.models.functions import TruncDate
    from django.utils import timezone

    from .models import PilotReadinessSnapshot

    if user is None:
        return []
    rows = (
        PilotReadinessSnapshot.objects.filter(
            character_id=character_id, user=user,
            created_at__gte=timezone.now() - timedelta(days=days),
        ).annotate(d=TruncDate("created_at")).values("d")
        .annotate(v=Avg("overall")).order_by("d")
    )
    return [round(r["v"]) for r in rows]


def _persist(user, character, overall, facets, recos) -> None:
    from .models import PilotReadinessSnapshot, PilotRecommendation

    PilotReadinessSnapshot.objects.create(
        character_id=character.character_id, user=user, overall=overall, facets=facets
    )

    seen: set[tuple] = set()
    for r in recos:
        key = (r["category"], r["ref_type"], r["ref_id"])
        seen.add(key)
        display = {
            "character_id": character.character_id,
            "title": r["title"], "detail": r["detail"],
            "priority": r["priority"], "points": r["points"], "action_url": r["action_url"],
        }
        obj, created = PilotRecommendation.objects.get_or_create(
            user=user, category=r["category"], ref_type=r["ref_type"], ref_id=r["ref_id"],
            defaults=display,
        )
        if not created:
            # Refresh the display fields but PRESERVE the pilot's state/snooze.
            for field, value in display.items():
                setattr(obj, field, value)
            obj.save(update_fields=[*display.keys(), "updated_at"])

    # An OPEN recommendation no longer generated means its gap closed (e.g. the pilot
    # trained the doctrine) → drop it. done/dismissed are kept (state preserved).
    for obj in PilotRecommendation.objects.filter(user=user, state=PilotRecommendation.State.OPEN):
        if (obj.category, obj.ref_type, obj.ref_id) not in seen:
            obj.delete()
