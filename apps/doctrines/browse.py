"""Build a flat, filterable catalogue of every active doctrine fit for pilots.

Each entry carries the hull class (for size filtering), the role/category, and —
when a character is given — how ready that pilot is to fly it (reusing the existing
readiness engine), so the view can filter by "can I fly it" and sort by "closest to
flying".
"""
from __future__ import annotations

from apps.sde.models import SdeType

from .hulls import hull_meta
from .models import Doctrine, DoctrineFit
from .services import character_readiness

STATUS_RANK = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}

# Lower is better: fly-optimal first, then fly-viable, then not-ready ordered by
# how few skills are missing, then no-data. Shared by the Doctrines library and the
# Shipyard so both default to "what this pilot can fly / is closest to flying".
_READINESS_ORDER = {"optimal": 0, "viable": 1, "not_ready": 2, "unknown": 3}


def readiness_sort_key(status: str | None, missing_count: int | None, tiebreak: str = "") -> tuple:
    """Sort key implementing the default pilot ordering.

    ``missing_count`` only ranks within *not-ready* rows (fewer missing skills =
    closer = earlier); it is ignored for fly-ready or unknown rows so a pilot who
    can already fly a doctrine always sorts ahead of one they cannot.
    """
    rank = _READINESS_ORDER.get(status or "unknown", 3)
    miss = missing_count if (status == "not_ready" and missing_count is not None) else 0
    return (rank, miss, str(tiebreak).lower())


def filter_library_rows(rows: list[dict], *, q: str = "", category: str = "",
                        hull: str = "", role: str = "", fly: str = "") -> list[dict]:
    """Apply the doctrine-library filters (search, category, hull, role, flyability) to
    :func:`apps.doctrines.library.build_library` rows.

    Shared by the doctrine library page and Tocha's Lab's doctrine picker so both filter
    identically. An empty filter is a no-op; ``fly`` needs a skill snapshot to mean
    anything (rows carry ``status`` only then)."""
    ql = (q or "").strip().lower()

    def keep(r: dict) -> bool:
        d = r["doctrine"]
        if ql and ql not in d.name.lower() and ql not in (d.description or "").lower():
            return False
        if category and str(r["category_id"]) != category:
            return False
        if hull and hull not in r["hull_classes"]:
            return False
        if role and role not in r["roles"]:
            return False
        if fly == "yes" and r["status"] not in ("optimal", "viable"):
            return False
        if fly == "no" and r["status"] != "not_ready":
            return False
        return True

    return [r for r in rows if keep(r)]


def enriched_fits(character=None, price_markup=None, with_availability=False) -> list[dict]:
    """Every active doctrine fit, enriched with hull class, role and (if a character
    is given) flyability + skill gap.

    When ``price_markup`` (a Decimal) is supplied — i.e. the Corp Store is enabled —
    each row is also priced at Jita sell × markup, reusing the store's pricing so
    the Shipyard is the single place to browse *and* buy a doctrine ship. When it is
    ``None`` the page is unchanged (no market lookups, no price shown).

    ``with_availability`` additionally attaches each row's availability (SHIP-1) from
    the store's ONE authoritative service — a constant number of batched queries for
    the whole page, never one per card. Rows gain an ``availability`` object (state,
    atp, eta, location, per-order maximum…) the template renders directly.
    """
    fits = list(
        DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE)
        .select_related("doctrine")
        .prefetch_related("skill_requirements")
        .order_by("doctrine__name", "name")
    )
    ship_ids = {f.ship_type_id for f in fits}
    names = dict(SdeType.objects.filter(type_id__in=ship_ids).values_list("type_id", "name"))
    meta = hull_meta(ship_ids)

    # Price EVERY fit in two batched queries, not one MarketPrice lookup per module per fit.
    # The Shipyard prices all active fits (200+), so the per-fit path was thousands of
    # queries / tens of seconds — a 504. Lazy import keeps doctrines importable without store.
    priced_map = {}
    if price_markup is not None:
        from apps.store.pricing import price_doctrine_fits_bulk

        priced_map = price_doctrine_fits_bulk(fits, price_markup)

    avail_map = {}
    if with_availability:
        from apps.store.availability import availability_for_fits

        avail_map = availability_for_fits(fits)

    # One snapshot fetch for the page instead of one per fit.
    snapshot = character.skill_snapshots.filter(is_latest=True).first() if character else None

    rows = []
    for f in fits:
        m = meta.get(f.ship_type_id, {})
        r = character_readiness(character, f, snapshot=snapshot) if character else None
        status = r.status if r else "unknown"
        unit_price, unit_jita = priced_map.get(f.id, (None, None))
        rows.append({
            "fit_id": f.id, "fit_name": f.name,
            "doctrine": f.doctrine.name, "doctrine_id": f.doctrine_id,
            "ship_type_id": f.ship_type_id,
            "ship_name": names.get(f.ship_type_id, f.name),
            "hull_class": m.get("hull_class", "Other"),
            "group_name": m.get("group_name", ""),
            "role": (f.role or "").strip(),
            "is_cheap_alt": f.is_cheap_alt,
            "unit_price": unit_price,
            "unit_jita": unit_jita,
            "availability": avail_map.get(f.id),
            "status": status,
            "can_fly": status in ("optimal", "viable"),
            "missing_count": len(r.missing_viable) if r else None,
            "_missing": r.missing_viable if r else [],
        })

    # Resolve missing-skill names once for nice tooltips, then drop the raw ids.
    skill_ids = {mr["skill_type_id"] for row in rows for mr in row["_missing"]}
    skill_names = dict(SdeType.objects.filter(type_id__in=skill_ids).values_list("type_id", "name"))
    for row in rows:
        row["missing_skills"] = [
            f"{skill_names.get(mr['skill_type_id'], mr['skill_type_id'])} {mr['need']}"
            for mr in row.pop("_missing")
        ]
    return rows


def filter_options(rows: list[dict]) -> dict:
    """Distinct hull classes and roles present, for the filter controls."""
    from .hulls import CLASS_ORDER

    classes = {r["hull_class"] for r in rows}
    roles = sorted({r["role"] for r in rows if r["role"]})
    return {
        "hull_classes": [c for c in CLASS_ORDER if c in classes],
        "roles": roles,
    }
