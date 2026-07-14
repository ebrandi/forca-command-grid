"""Doctrine services: fit creation, skill-requirement derivation, readiness, coverage."""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.sde.models import SdeTypeSkill

# The key + its shipped English label live in category_i18n (the render-time seam's
# catalogue), so the seeded label and the msgid can never drift apart.
from .category_i18n import BUILTIN_CATEGORY_LABELS, IMPORTED_CATEGORY_KEY
from .fitparser import export_eft
from .models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement


def match_doctrine_fit(ship_type_id: int) -> DoctrineFit | None:
    """The DoctrineFit of the highest-priority active doctrine whose hull matches
    ``ship_type_id`` (or None). The returned fit has its ``.doctrine`` cached so
    callers can read it without an extra query. Shared by killboard doctrine
    tagging (KB-13) and SRP eligibility."""
    for doctrine in (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits")
        .order_by("-priority", "name")
    ):
        for fit in doctrine.fits.all():
            if fit.ship_type_id == ship_type_id:
                fit.doctrine = doctrine
                return fit
    return None


def _fit_module_types(modules) -> set[int]:
    """The set of FITTED-slot module type_ids in a ``DoctrineFit.modules`` list.

    Cargo / spare-hold items are dropped where the slot is known, and quantities are
    ignored entirely — so a fit's high-quantity cargo ammo (the importers aggregate
    cargo spares, thousands of units) cannot dominate fit matching. Non-dict entries in
    corrupt stored JSON are skipped rather than aborting the caller.
    """
    types: set[int] = set()
    for module in modules or []:
        if not isinstance(module, dict):
            continue
        if str(module.get("slot") or "").lower().startswith("cargo"):
            continue
        tid = module.get("type_id")
        if tid:
            types.add(int(tid))
    return types


def best_doctrine_fit(ship_type_id: int, fitted: dict[int, int] | None) -> DoctrineFit | None:
    """The active doctrine fit for this hull that best matches the ACTUALLY fitted
    modules — so a multi-fit hull tags to the variant the pilot was flying, not just the
    first by priority (4.2). ``fitted`` is a ``{type_id: quantity}`` multiset of the
    fitted-slot modules (from the loss / a live fit).

    Degrades to :func:`match_doctrine_fit`'s hull-only result when the hull has one (or
    zero) candidate fit, or when there is no fitted data — so single-fit hulls and
    fitless killmails are byte-for-byte unchanged. Among multiple same-hull candidates it
    picks the one with the fewest differing **module types** vs the fitted set — a
    quantity-insensitive metric so spare-ammo mass can't select the wrong fit (a fit's
    guns/mods discriminate the variant, its cargo ammo can't swamp the signal). Ties keep
    doctrine priority order (``min`` returns the first minimum, and the candidate list is
    already priority-ordered). The returned fit has its ``.doctrine`` cached, like
    ``match_doctrine_fit``.
    """
    candidates: list[tuple[DoctrineFit, Doctrine]] = []
    for doctrine in (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits")
        .order_by("-priority", "name")
    ):
        for fit in doctrine.fits.all():
            if fit.ship_type_id == ship_type_id:
                candidates.append((fit, doctrine))
    if not candidates:
        return None
    if len(candidates) == 1 or not fitted:
        fit, doctrine = candidates[0]
        fit.doctrine = doctrine
        return fit

    fitted_types = set(fitted)

    def _diff(fit: DoctrineFit) -> int:
        # Distinct module types that differ (symmetric difference), quantity-insensitive.
        return len(_fit_module_types(fit.modules) ^ fitted_types)

    fit, doctrine = min(candidates, key=lambda c: _diff(c[0]))
    fit.doctrine = doctrine
    return fit


def imported_category() -> DoctrineCategory:
    """The IMPORTED category — the default home for ESI-imported fits. Seeded by
    a migration; get_or_create here keeps the import robust if it was removed."""
    cat, _ = DoctrineCategory.objects.get_or_create(
        key=IMPORTED_CATEGORY_KEY,
        # Canonical ENGLISH in the column (the audit record + fallback); it is translated
        # at render time by DoctrineCategory.label_i18n, keyed on ``key``.
        defaults={"label": BUILTIN_CATEGORY_LABELS[IMPORTED_CATEGORY_KEY], "sort_order": 100},
    )
    return cat


def fit_signature(ship_type_id: int, modules: list[dict]) -> tuple:
    """A canonical identity for a fit: hull + the multiset of (type_id, quantity).

    Quantities are aggregated by type so the same fit compares equal regardless of
    how its modules were stored (ESI aggregates per type; an EFT paste may list the
    same module on several lines). Module ``name``/``slot`` are ignored — only what
    is fitted, and how many, defines the fit.
    """
    agg: dict[int, int] = {}
    for m in modules or []:
        tid = m.get("type_id")
        if tid is None:
            continue
        agg[int(tid)] = agg.get(int(tid), 0) + int(m.get("quantity", 1) or 1)
    return (int(ship_type_id), frozenset(agg.items()))


def name_conflict(name: str, ship_type_id: int, modules: list[dict]):
    """Classify importing a fit named ``name`` against existing doctrines.

    Returns one of:
      ``("duplicate", doctrine)`` — a doctrine with this name already holds an
          identical fit (same hull + modules): the import is a no-op.
      ``("conflict", doctrine)`` — a doctrine with this name exists but with a
          different fit: the importer must rename to avoid two same-named doctrines.
      ``(None, None)`` — the name is free; safe to create.

    Name match is case-insensitive so near-identical names don't slip through.
    """
    sig = fit_signature(ship_type_id, modules)
    existing = list(Doctrine.objects.filter(name__iexact=name.strip()).prefetch_related("fits"))
    if not existing:
        return None, None
    for doctrine in existing:
        if any(fit_signature(f.ship_type_id, f.modules) == sig for f in doctrine.fits.all()):
            return "duplicate", doctrine
    return "conflict", existing[0]


def create_fit(
    doctrine: Doctrine,
    *,
    name: str,
    ship_type_id: int,
    modules: list[dict],
    role: str = "",
    is_cheap_alt: bool = False,
    eft_text: str = "",
) -> DoctrineFit:
    """Create a DoctrineFit from a normalised module list and derive its skills.

    Shared by every import path (EFT paste, ESI saved fits, killmail) so a fit
    always lands the same way: stored modules + an EFT round-trip for display +
    auto-derived skill requirements. ``eft_text`` is regenerated from the stored
    modules when not supplied, so exports stay consistent.
    """
    fit = DoctrineFit.objects.create(
        doctrine=doctrine,
        name=name,
        ship_type_id=ship_type_id,
        role=role,
        modules=modules,
        is_cheap_alt=is_cheap_alt,
        eft_text=eft_text,
    )
    if not eft_text:
        fit.eft_text = export_eft(fit)
        fit.save(update_fields=["eft_text"])
    derive_skill_requirements(fit)
    return fit


def update_fit(
    fit: DoctrineFit,
    *,
    name: str | None = None,
    modules: list[dict] | None = None,
    role: str | None = None,
    eft_text: str | None = None,
    is_cheap_alt: bool | None = None,
) -> DoctrineFit:
    """Replace an existing fit's contents **in place**, preserving its row id.

    Used by the "replace existing" branch of an import so every foreign key that
    points at this fit — loss deviations (KB-13), operation slot assignments,
    requirement rows — keeps working instead of being cascade-deleted. The
    doctrine and fit ids are untouched; only the fitted contents change, and skill
    requirements are re-derived from the new module list.
    """
    if name is not None:
        fit.name = name
    if modules is not None:
        fit.modules = modules
    if role is not None:
        fit.role = role
    if is_cheap_alt is not None:
        fit.is_cheap_alt = is_cheap_alt
    # Regenerate the EFT round-trip from the (possibly new) modules unless caller
    # supplied one, so the stored text never drifts from the stored modules.
    fit.eft_text = eft_text if eft_text else export_eft(fit)
    fit.save()
    derive_skill_requirements(fit)
    return fit


def derive_skill_requirements(fit: DoctrineFit) -> int:
    """(Re)derive SkillRequirement rows for a fit from SDE dogma data.

    Requirements come from the ship hull plus every fitted module's required
    skills (taking the highest level required across the fit). Manual overrides
    (derived_from=MANUAL) are preserved.
    """
    type_ids = {fit.ship_type_id}
    for module in fit.modules or []:
        tid = module.get("type_id")
        if tid:
            type_ids.add(int(tid))

    required: dict[int, int] = {}
    for row in SdeTypeSkill.objects.filter(type_id__in=type_ids):
        skill_id = row.skill_type_id
        required[skill_id] = max(required.get(skill_id, 0), row.level)

    manual = set(
        fit.skill_requirements.filter(
            derived_from=SkillRequirement.DerivedFrom.MANUAL
        ).values_list("skill_type_id", flat=True)
    )
    # Replace auto rows only.
    fit.skill_requirements.filter(
        derived_from=SkillRequirement.DerivedFrom.AUTO_DOGMA
    ).delete()
    created = 0
    for skill_id, level in required.items():
        if skill_id in manual:
            continue
        SkillRequirement.objects.update_or_create(
            fit=fit,
            skill_type_id=skill_id,
            defaults={
                "min_level": level,
                "optimal_level": level,
                "derived_from": SkillRequirement.DerivedFrom.AUTO_DOGMA,
            },
        )
        created += 1
    return created


@dataclass
class FitReadiness:
    fit_id: int
    fit_name: str
    status: str  # "optimal" | "viable" | "not_ready" | "unknown"
    missing_viable: list[dict] = field(default_factory=list)
    missing_optimal: list[dict] = field(default_factory=list)


_SNAPSHOT_UNSET = object()


def character_readiness(character, fit: DoctrineFit, snapshot=_SNAPSHOT_UNSET) -> FitReadiness:
    """Compare a character's latest skills against a fit's requirements.

    ``snapshot`` may be passed pre-loaded (the character's latest
    ``CharacterSkillSnapshot`` or ``None``) so a caller scoring many fits for the
    same character avoids one snapshot query per call — the readiness/doctrines
    hot paths do this. Omit it and the snapshot is fetched as before.
    """
    if snapshot is _SNAPSHOT_UNSET:
        snapshot = character.skill_snapshots.filter(is_latest=True).first()
    reqs = list(fit.skill_requirements.all())
    if snapshot is None:
        # Honest data: an un-imported character is "unknown", never "not ready".
        return FitReadiness(fit.id, fit.name, "unknown")
    if not reqs:
        # No requirements derived yet (e.g. derivation not run / SDE lacked
        # dogma) — report "unknown" rather than falsely claiming everyone can fly.
        return FitReadiness(fit.id, fit.name, "unknown")

    missing_viable: list[dict] = []
    missing_optimal: list[dict] = []
    for req in reqs:
        trained = snapshot.trained_level(req.skill_type_id)
        if trained < req.min_level:
            missing_viable.append(
                {"skill_type_id": req.skill_type_id, "have": trained, "need": req.min_level}
            )
        if trained < req.optimal_level:
            missing_optimal.append(
                {"skill_type_id": req.skill_type_id, "have": trained, "need": req.optimal_level}
            )

    if missing_viable:
        status = "not_ready"
    elif missing_optimal:
        status = "viable"
    else:
        status = "optimal"
    return FitReadiness(fit.id, fit.name, status, missing_viable, missing_optimal)


def readiness_summary_for_character(character) -> list[dict]:
    """Per-doctrine readiness for a character (best fit per doctrine)."""
    summary: list[dict] = []
    doctrines = (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements")
        .order_by("-priority", "name")
    )
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    # One snapshot fetch for the whole page instead of one per fit.
    snapshot = character.skill_snapshots.filter(is_latest=True).first()
    for doctrine in doctrines:
        best: FitReadiness | None = None
        for fit in doctrine.fits.all():
            r = character_readiness(character, fit, snapshot=snapshot)
            if best is None or rank[r.status] > rank[best.status]:
                best = r
        if best is not None:
            summary.append(
                {
                    "doctrine_id": doctrine.id,
                    "doctrine": doctrine.name,
                    "status": best.status,
                    "fit": best.fit_name,
                    "missing_viable": best.missing_viable,
                }
            )
    return summary


def _level_in(skills_map: dict, skill_type_id: int) -> int:
    """Trained level of a skill in a raw snapshot ``skills`` dict (keys may be
    str or int), 0 if absent."""
    entry = skills_map.get(str(skill_type_id)) or skills_map.get(skill_type_id)
    return int(entry.get("trained_level", 0)) if entry else 0


def flyable_doctrine_ids(skills_map: dict) -> set[int]:
    """Ids of active doctrines flyable with the given snapshot ``skills`` dict.

    A doctrine is flyable if ANY of its fits has every (derived) skill requirement
    met at its ``min_level``. Fits with no derived requirements don't count — we
    never claim a doctrine is unlocked on missing data (matches ``character_readiness``
    treating those as 'unknown'). Used to detect *newly* unlocked doctrines by
    diffing the set before/after a skill import.
    """
    ids: set[int] = set()
    doctrines = (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements")
    )
    for doctrine in doctrines:
        for fit in doctrine.fits.all():
            reqs = list(fit.skill_requirements.all())
            if reqs and all(_level_in(skills_map, r.skill_type_id) >= r.min_level for r in reqs):
                ids.add(doctrine.id)
                break
    return ids


def doctrine_required_sp(doctrine: Doctrine) -> int:
    """Total SP to train the doctrine's *easiest* fit from scratch — a stable
    measure of how hard the doctrine is to unlock (used to weight unlock points)."""
    from apps.sde.models import SdeType
    from apps.skills.services import sp_between_levels

    best: int | None = None
    for fit in doctrine.fits.all():
        reqs = list(fit.skill_requirements.all())
        if not reqs:
            continue
        ranks = dict(
            SdeType.objects.filter(type_id__in=[r.skill_type_id for r in reqs])
            .values_list("type_id", "rank")
        )
        total = sum(
            sp_between_levels(ranks.get(r.skill_type_id, 1) or 1, 0, r.min_level)
            for r in reqs
        )
        if best is None or total < best:
            best = total
    return best or 0


def doctrine_coverage(doctrine: Doctrine, characters) -> dict:
    """How many of the given characters can fly the doctrine (best fit)."""
    counts = {"optimal": 0, "viable": 0, "not_ready": 0, "unknown": 0}
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    snaps = _latest_snapshots(characters)
    # Materialise the fits (with their skill requirements) ONCE, not per character —
    # otherwise doctrine.fits.all() + fit.skill_requirements re-query for every character,
    # and this runs per-doctrine per-op on the Operations list (the critical N+1).
    fits = list(doctrine.fits.prefetch_related("skill_requirements"))
    for character in characters:
        best_status = "unknown"
        snapshot = snaps.get(character.character_id)
        for fit in fits:
            status = character_readiness(character, fit, snapshot=snapshot).status
            if rank[status] > rank[best_status]:
                best_status = status
        counts[best_status] += 1
    return counts


def _latest_snapshots(characters) -> dict:
    """``{character_id: latest CharacterSkillSnapshot}`` in a single query."""
    from apps.characters.models import CharacterSkillSnapshot

    return {
        s.character_id: s
        for s in CharacterSkillSnapshot.objects.filter(
            is_latest=True, character_id__in=[c.character_id for c in characters]
        )
    }


# --- DOC-2 (2.5): cached corp-wide doctrine coverage dashboard ---------------
_COVERAGE_CACHE_VERSION = 1
_COVERAGE_TTL = 900  # 15 min — member skills sync at most every 12h.


def _coverage_cache_key(characters) -> str:
    """Versioned on the members' latest-snapshot time + the active-doctrine set, so the
    dashboard self-invalidates on a fresh sync, doctrine change or roster change.

    Language-scoped (D17): the cached rows carry prose — the doctrine ``name`` and the
    render-time-translated ``category`` label — so a German pilot's payload must not be
    served to an English one. The key self-invalidates, so no cross-language sweep is
    needed on write.
    """
    import hashlib

    from django.db.models import Max

    from apps.characters.models import CharacterSkillSnapshot
    from core.i18n import i18n_cache_key

    member_ids = sorted(c.character_id for c in characters)
    latest = CharacterSkillSnapshot.objects.filter(
        is_latest=True, character_id__in=member_ids
    ).aggregate(m=Max("as_of"))["m"]
    doc_ids = sorted(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).values_list("id", flat=True)
    )
    sig = hashlib.sha256(f"{member_ids}|{doc_ids}".encode()).hexdigest()[:16]
    return i18n_cache_key(
        f"doctrines:coverage:{_COVERAGE_CACHE_VERSION}:"
        f"{int(latest.timestamp() * 1_000_000) if latest else 0}:{sig}"
    )


def corp_doctrine_coverage(characters) -> list[dict]:
    """Per active doctrine: optimal / viable / not-ready / unknown pilot counts.

    Snapshots are loaded once and threaded through (no per-doctrine re-query), and the
    whole result is cached — the coverage engine is O(doctrines × fits × members), so the
    leadership dashboard must not recompute it on every request. Sorted by priority.
    """
    from django.core.cache import cache

    key = _coverage_cache_key(characters)
    cached = cache.get(key)
    if cached is not None:
        return cached

    snaps = _latest_snapshots(characters)
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    total = len(characters)
    rows = []
    doctrines = (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .select_related("category")
        .prefetch_related("fits__skill_requirements")
        .order_by("-priority", "name")
    )
    for doctrine in doctrines:
        counts = {"optimal": 0, "viable": 0, "not_ready": 0, "unknown": 0}
        fits = list(doctrine.fits.all())
        for character in characters:
            snapshot = snaps.get(character.character_id)
            best = "unknown"
            for fit in fits:
                status = character_readiness(character, fit, snapshot=snapshot).status
                if rank[status] > rank[best]:
                    best = status
            counts[best] += 1
        rows.append({
            "doctrine_id": doctrine.id,
            "name": doctrine.name,
            "priority": doctrine.priority or 0,
            "category": doctrine.category.label_i18n if doctrine.category else "",
            **counts,
            "can_fly": counts["optimal"] + counts["viable"],
            "total": total,
        })
    cache.set(key, rows, _COVERAGE_TTL)
    return rows
