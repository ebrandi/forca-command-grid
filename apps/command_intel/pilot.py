"""Pilot Intelligence — the strategic quest log (design doc 16 §7).

Answers CI's second question for a member: *"what single thing can I do today that most
strengthens my corporation?"* It projects the corp's binding **operational constraints**
down to the one move **this** pilot can make to relieve one of them — a corp shortage
turned into a personal opportunity. Ranking is CI-aware: a directive's ``leverage`` is the
severity of the constraint it relieves, so the top card is genuinely the highest-leverage
personal action, not a generic suggestion.

Directives are upserted by ``(user, slug)`` so the pilot's done/snooze/dismiss state
survives regeneration (the readiness ``PilotRecommendation`` pattern); an OPEN directive
whose underlying constraint no longer binds is dropped. When CI is thin (no snapshot, no
binding constraints, or the pilot can't personally help any), it falls back to a
training-toward-doctrines seed so the log is never empty and never fabricates a
corp-impact claim.
"""
from __future__ import annotations

from . import messages
from .models import PilotDirective
from .snapshot import latest_snapshot

_CACHE_TTL = 1800            # 30 min — recompute is constraint reads + a skills scan
_MAX_DIRECTIVES = 8
# Severity → ranking weight (the corp-impact of relieving this constraint).
_SEV_WEIGHT = {"critical": 90, "high": 70, "watch": 45, "info": 20}
# Constraint families a member can personally move (financial/infra levers are officer COAs).
_PILOT_RELIEVABLE = ("fleet_size.", "doctrine_stock.")


def cache_key(character_id) -> str:
    return f"command_intel:pilot:{character_id}"


def open_directives(user) -> list:
    """The member's currently-actionable directives (OPEN and not snoozed)."""
    from django.db.models import Q
    from django.utils import timezone

    now = timezone.now()
    return list(
        PilotDirective.objects.filter(user=user, state=PilotDirective.State.OPEN)
        .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now))
    )


def _eta_ref(seconds: int) -> dict:
    """The training ETA as a composable scaffold ref (Seam B) — it is prose inside a sentence.

    ``under an hour`` is plainly translatable; the ``3d``/``5h`` unit suffixes are too (a locale may
    abbreviate differently), so all three are msgids rather than a bare formatted number.
    """
    days = seconds // 86400
    if days >= 1:
        return messages.ref("pilot.eta.days", {"n": days})
    hours = seconds // 3600
    if hours >= 1:
        return messages.ref("pilot.eta.hours", {"n": hours})
    return messages.ref("pilot.eta.under_hour")


def _humanize_eta(seconds: int) -> str:
    """The English ETA — derived from the same msgid, so the two can never drift."""
    return _eta_ref(seconds)["_en"]


def _doctrine_names(snapshot) -> dict[str, str]:
    """``slug → display name`` from the snapshot's doctrine slice."""
    if snapshot is None:
        return {}
    rows = (snapshot.slices.get("doctrine") or {}).get("doctrines") or []
    return {d.get("slug"): d.get("name") for d in rows if isinstance(d, dict) and d.get("slug")}


def _open_constraints(snapshot) -> list:
    """Computed, binding (watch+) constraints for the snapshot, most-binding first."""
    from .models import OperationalConstraint

    if snapshot is None:
        return []
    rows = [
        c for c in OperationalConstraint.objects.filter(snapshot=snapshot)
        if c.status == "computed" and c.severity in ("watch", "high", "critical")
    ]
    return sorted(rows, key=lambda c: _SEV_WEIGHT.get(c.severity, 0), reverse=True)


def _directive(**kw) -> dict:
    kw.setdefault("constraint_key", "")
    kw.setdefault("posture_lift", None)
    # Seam B: every directive carries its sentences as key + JSON-safe params as well as English.
    kw.setdefault("title_key", "")
    kw.setdefault("title_params", {})
    kw.setdefault("detail_key", "")
    kw.setdefault("detail_params", {})
    return kw


def _relief_directives(character, constraints, names) -> list[dict]:
    """One directive per binding constraint this pilot can personally relieve."""
    from django.utils.text import slugify

    from apps.skills.services import closest_doctrines

    # Doctrines this pilot is closest to flying, keyed by slug (matches the constraint key).
    closest = {slugify(d["doctrine"]): d for d in closest_doctrines(character, limit=8)}
    out: list[dict] = []
    staged: set[str] = set()
    for c in constraints:
        if not c.key.startswith(_PILOT_RELIEVABLE):
            continue
        slug = c.key.split(".", 1)[1]
        name = names.get(slug) or slug
        weight = _SEV_WEIGHT.get(c.severity, 0)
        metric = f"{c.binding_metric:g}" if c.binding_metric is not None else "—"

        # ``c`` here is the persisted OperationalConstraint row, so its label arrives as a
        # (label, label_key, label_params) triple — embed the SENTENCE as a ref, not the frozen prose.
        label_ref = c.label_ref()

        if c.limiting_factor == "pilots_qualified" and slug in closest:
            info = closest[slug]
            title_params = {"doctrine": info["doctrine"]}
            detail_params = {
                "constraint": label_ref, "metric": metric, "eta": _eta_ref(info["seconds"]),
            }
            out.append(_directive(
                slug=f"{c.key}/train", constraint_key=c.key,
                category=PilotDirective.Category.SKILL,
                title=messages.english("pilot.directive.train.title", title_params),
                title_key="pilot.directive.train.title", title_params=title_params,
                detail=messages.english("pilot.directive.train.detail", detail_params),
                detail_key="pilot.directive.train.detail", detail_params=detail_params,
                leverage=weight + 5, points=12, action_url="/skills/",
            ))
        elif c.limiting_factor == "hulls_in_stock" and slug not in staged:
            staged.add(slug)
            title_params = {"hull": name}
            detail_params = {"constraint": label_ref, "hull": name}
            out.append(_directive(
                slug=f"{c.key}/stage-hull", constraint_key=c.key,
                category=PilotDirective.Category.SHIP,
                title=messages.english("pilot.directive.stage_hull.title", title_params),
                title_key="pilot.directive.stage_hull.title", title_params=title_params,
                detail=messages.english("pilot.directive.stage_hull.detail", detail_params),
                detail_key="pilot.directive.stage_hull.detail", detail_params=detail_params,
                leverage=weight, points=8, action_url="/store/",
            ))
    return out


def _fallback_directives(character) -> list[dict]:
    """Honest seed when no constraint-relief move applies: train toward the doctrines."""
    from apps.skills.services import closest_doctrines

    out: list[dict] = []
    for i, d in enumerate(closest_doctrines(character, limit=4)):
        title_params = {"doctrine": d["doctrine"]}
        detail_params = {"eta": _eta_ref(d["seconds"]), "doctrine": d["doctrine"]}
        out.append(_directive(
            slug=f"doctrine/{d['doctrine_id']}",
            category=PilotDirective.Category.SKILL,
            title=messages.english("pilot.directive.train.title", title_params),
            title_key="pilot.directive.train.title", title_params=title_params,
            detail=messages.english("pilot.directive.fallback_train.detail", detail_params),
            detail_key="pilot.directive.fallback_train.detail", detail_params=detail_params,
            leverage=0, points=max(2, 10 - i * 2), action_url="/skills/",
        ))
    if not out:
        out.append(_directive(
            slug="stay-current/join-op", category=PilotDirective.Category.ROLE,
            title=messages.english("pilot.directive.join_op.title"),
            title_key="pilot.directive.join_op.title",
            detail=messages.english("pilot.directive.join_op.detail"),
            detail_key="pilot.directive.join_op.detail",
            leverage=0, points=4, action_url="/operations/",
        ))
    return out


def compute_directives(user, character, *, persist: bool = True) -> dict:
    """Rank this pilot's corp-aligned directives and (optionally) persist the quest log."""
    from django.core.cache import cache

    snapshot = latest_snapshot()
    names = _doctrine_names(snapshot)
    constraints = _open_constraints(snapshot)

    directives = _relief_directives(character, constraints, names) or _fallback_directives(character)
    directives.sort(key=lambda d: (d["leverage"], d["points"]), reverse=True)
    directives = directives[:_MAX_DIRECTIVES]

    if persist:
        _persist(user, directives)

    payload = {
        "directives": directives,
        "binding_count": len(constraints),
        "top_constraint": (constraints[0].label if constraints else None),
        "ci_grounded": bool(constraints) and any(d.get("constraint_key") for d in directives),
    }
    cache.set(cache_key(character.character_id), payload, _CACHE_TTL)
    return payload


def _persist(user, directives: list[dict]) -> None:
    """Upsert directives by ``(user, slug)``, preserving state; drop stale OPEN ones."""
    seen: set[str] = set()
    for d in directives:
        seen.add(d["slug"])
        display = {
            "constraint_key": d.get("constraint_key", ""),
            "category": d["category"],
            "title": d["title"][:200],
            "detail": d["detail"],
            # Seam B: the key + params move in lockstep with the prose they describe.
            "title_key": d.get("title_key", ""),
            "title_params": d.get("title_params") or {},
            "detail_key": d.get("detail_key", ""),
            "detail_params": d.get("detail_params") or {},
            "leverage": d["leverage"],
            "points": d["points"],
            "posture_lift": d.get("posture_lift"),
            "action_url": d.get("action_url", ""),
        }
        obj, created = PilotDirective.objects.get_or_create(
            user=user, slug=d["slug"], defaults=display,
        )
        if not created:
            # Refresh the display fields but PRESERVE the pilot's state/snooze.
            for field, value in display.items():
                setattr(obj, field, value)
            obj.save(update_fields=[*display.keys(), "updated_at"])

    # An OPEN directive no longer generated means its constraint stopped binding → drop it.
    # done/dismissed/snoozed are kept (state preserved).
    for obj in PilotDirective.objects.filter(user=user, state=PilotDirective.State.OPEN):
        if obj.slug not in seen:
            obj.delete()
