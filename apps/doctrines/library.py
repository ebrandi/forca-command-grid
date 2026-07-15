"""Doctrine library overview for the main /doctrines/ page.

In a single pass over every active doctrine this builds three things the list
page needs:

* **Facets** — the hull classes and roles each doctrine spans (folded up from its
  fits), plus the option lists that drive the filter controls.
* **Corp statistics** — how the library breaks down by hull class and category, so
  a pilot can see at a glance what the corp actually flies.
* **A training plan** — when a pilot is selected, which skills would unlock the
  most doctrine fits they cannot yet fly, so training effort goes where it buys
  the most options. This reuses the existing readiness engine.

Stats describe the WHOLE library (a corp overview); the view filters the list
rows separately so a narrow filter never makes the overview lie.
"""
from __future__ import annotations

from collections import defaultdict

from django.utils.translation import gettext as _

from apps.sde.models import SdeType

from .hulls import CLASS_ORDER, hull_meta
from .models import Doctrine
from .services import character_readiness

# How many training-priority skills to surface in the chart/list.
TOP_SKILLS = 12

_RANK = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
_CLASS_INDEX = {c: i for i, c in enumerate(CLASS_ORDER)}


def _class_key(hull_class: str) -> int:
    return _CLASS_INDEX.get(hull_class, len(CLASS_ORDER))


def build_library(character=None, has_skills: bool = False) -> dict:
    """Assemble the doctrine rows, filter options, and statistics for the page.

    ``character`` (optional) is the pilot whose readiness drives the per-doctrine
    flyability badge, the readiness donut, and the training plan. ``has_skills``
    says whether that character has an imported skill snapshot — when False the
    pilot-specific panels invite the member to import rather than claiming, e.g.,
    that they can fly nothing.
    """
    doctrines = list(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .select_related("category")
        .prefetch_related("fits__skill_requirements")
        .order_by("-priority", "name")
    )

    ship_ids = {f.ship_type_id for d in doctrines for f in d.fits.all()}
    meta = hull_meta(ship_ids)

    # Load the pilot's latest skill snapshot ONCE (fits are already prefetched above), so
    # character_readiness below doesn't issue one snapshot query per fit across the library.
    snapshot = (
        character.skill_snapshots.filter(is_latest=True).first()
        if has_skills and character is not None
        else None
    )

    hull_counts: dict[str, int] = defaultdict(int)        # fits by hull class
    category_counts: dict[str, int] = defaultdict(int)    # doctrines by category
    readiness_counts = {"optimal": 0, "viable": 0, "not_ready": 0, "unknown": 0}

    # Training-plan accumulators (only meaningful with a skill snapshot).
    blocks: dict[int, int] = defaultdict(int)    # skill -> # fits it blocks
    unlocks: dict[int, int] = defaultdict(int)   # skill -> # fits it alone gates
    weight: dict[int, int] = defaultdict(int)    # skill -> sum of doctrine priority

    role_set: set[str] = set()
    total_fits = 0
    rows = []

    for d in doctrines:
        fits = list(d.fits.all())
        total_fits += len(fits)
        d_hulls: set[str] = set()
        d_roles: set[str] = set()
        best_status = "unknown"
        closest_missing: int | None = None  # fewest skills missing across not-ready fits

        for f in fits:
            hull_class = meta.get(f.ship_type_id, {}).get("hull_class", "Other")
            d_hulls.add(hull_class)
            hull_counts[hull_class] += 1
            role = (f.role or "").strip()
            if role:
                d_roles.add(role)
                role_set.add(role)

            if has_skills:
                r = character_readiness(character, f, snapshot=snapshot)
                if _RANK[r.status] > _RANK[best_status]:
                    best_status = r.status
                if r.status == "not_ready":
                    gap = len(r.missing_viable)
                    closest_missing = gap if closest_missing is None else min(closest_missing, gap)
                    sole = gap == 1
                    for miss in r.missing_viable:
                        sid = miss["skill_type_id"]
                        blocks[sid] += 1
                        weight[sid] += max(d.priority, 1)
                        if sole:
                            unlocks[sid] += 1

        # Display-only aggregation key (it feeds the "By category" doughnut's labels), so
        # the translated label is correct here — nothing is ever looked up by it. The
        # doctrine's own category is matched on ``category_id``, never on this text.
        category_label = d.category.label_i18n if d.category else _("Uncategorised")
        category_counts[category_label] += 1
        if has_skills:
            readiness_counts[best_status] += 1

        rows.append({
            "doctrine": d,
            "thumb": fits[0].ship_type_id if fits else None,
            "hull_classes": sorted(d_hulls, key=_class_key),
            "roles": sorted(d_roles),
            "fit_count": len(fits),
            "category_id": d.category_id,
            "category_label": d.category.label_i18n if d.category else "",
            "status": best_status if has_skills else None,
            # Fewest skills the pilot is missing on their closest fit (only when the
            # best they can do is "not ready"); drives the "closest to fly" ordering.
            "missing_count": closest_missing if best_status == "not_ready" else None,
        })

    # --- Filter option lists ---
    present_classes = set(hull_counts)
    categories = [
        {"id": d.category_id, "label": d.category.label_i18n}
        for d in doctrines
        if d.category_id
    ]
    # Distinct, preserving the model's sort order.
    seen: set[int] = set()
    category_options = []
    for c in categories:
        if c["id"] not in seen:
            seen.add(c["id"])
            category_options.append(c)

    hull_options = [c for c in CLASS_ORDER if c in present_classes]
    role_options = sorted(role_set)

    # --- Chart payloads ---
    stats = {
        "hull": [
            {"name": c, "count": hull_counts[c]}
            for c in CLASS_ORDER
            if hull_counts.get(c)
        ],
        "category": sorted(
            ({"name": k, "count": v} for k, v in category_counts.items()),
            key=lambda r: -r["count"],
        ),
    }

    readiness = {
        "configured": has_skills,
        "optimal": readiness_counts["optimal"],
        "viable": readiness_counts["viable"],
        "not_ready": readiness_counts["not_ready"],
        "unknown": readiness_counts["unknown"],
    }

    # --- Training plan ---
    skill_names = dict(
        SdeType.objects.filter(type_id__in=set(blocks)).values_list("type_id", "name")
    )
    ranked = sorted(
        blocks,
        key=lambda s: (unlocks[s], blocks[s], weight[s]),
        reverse=True,
    )[:TOP_SKILLS]
    priority = {
        "configured": has_skills,
        "skills": [
            {
                "name": skill_names.get(s) or _("Skill %(type_id)s") % {"type_id": s},
                "blocks": blocks[s],
                "unlocks": unlocks[s],
            }
            for s in ranked
        ],
    }

    can_fly = readiness_counts["optimal"] + readiness_counts["viable"]
    headline = {
        "doctrines": len(doctrines),
        "fits": total_fits,
        "categories": len(category_options),
        "hulls": len(hull_options),
        "can_fly": can_fly,
    }

    return {
        "rows": rows,
        "categories": category_options,
        "hull_classes": hull_options,
        "roles": role_options,
        "stats": stats,
        "readiness": readiness,
        "priority": priority,
        "headline": headline,
    }
