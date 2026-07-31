"""Doctrine services: fit creation, skill-requirement derivation, readiness, coverage."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db.models import prefetch_related_objects
from django.db.models.signals import post_delete, post_save
from django.utils.translation import gettext_lazy as _

from apps.sde.models import SdeTypeSkill

# The key + its shipped English label live in category_i18n (the render-time seam's
# catalogue), so the seeded label and the msgid can never drift apart.
from .category_i18n import BUILTIN_CATEGORY_LABELS, IMPORTED_CATEGORY_KEY
from .fitparser import export_eft
from .models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement


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


# --------------------------------------------------------------------------- #
#  The active-doctrine catalogue snapshot (audit P4/P6/P8)
#
#  ``best_doctrine_fit`` / ``match_doctrine_fit`` are the hull→fit authority for the
#  whole product, and every caller is a LOOP: campaign analytics score one match per
#  loss (thousands per board, reachable anonymously), SRP eligibility matches once per
#  claim row, ingest tags every killmail. Each call used to re-materialise the ENTIRE
#  active catalogue — two Postgres round-trips plus full ORM construction of every
#  Doctrine and every DoctrineFit, including detoasting each fit's ``modules`` JSONB —
#  to answer one integer question. That is the single most-repeated wasted query in
#  the codebase.
#
#  So we keep a process-local snapshot, exactly like the market app's ``price_for``
#  (``apps/market/pricing.py``): after the first call in a window, matching is pure
#  Python over an in-memory index.
#
#  Two things make this snapshot SAFER than a bare ``lru_cache``, and both are
#  deliberate — a stale doctrine catalogue silently mis-tags losses and mis-pays SRP,
#  which is far worse than a slow page:
#
#  1. **Staleness is bounded by a SHARED stamp, not just a TTL.** Under gunicorn every
#     worker holds its own snapshot, so an officer editing a doctrine in worker A would
#     never reach workers B..N. Each refresh therefore reads a small stamp from the
#     shared cache and rebuilds whenever it moved; ``Doctrine``/``DoctrineFit`` saves and
#     deletes bump that stamp (see the signal wiring at the bottom of this module). A
#     doctrine edit is visible everywhere on the next match, not "eventually". The
#     ``_CATALOGUE_TTL`` is only the backstop for writes that bypass signals (a bulk
#     ``queryset.update()``, a manual SQL fix), and is deliberately far shorter than the
#     market snapshot's 300 s because this data feeds payouts rather than estimates.
#  2. **Callers never see the cached objects.** ``best_doctrine_fit`` documents that it
#     returns a fit with ``.doctrine`` attached — i.e. callers MUTATE the object they get
#     back, and SRP/ingest also hand it to ``killmail.doctrine_fit = fit``. Handing out a
#     shared instance would let one request's mutation leak into another's. The snapshot
#     therefore stores plain field VALUES and rebuilds a fresh model instance (deep-copied,
#     so the ``modules`` list is private too) for the one fit actually returned.
# --------------------------------------------------------------------------- #
_CATALOGUE_TTL = 30.0  # seconds; backstop for signal-bypassing writes only
_CATALOGUE_STAMP_KEY = "doctrines:catalogue-stamp:1"
_CATALOGUE_LOCK = threading.Lock()
_CATALOGUE: dict = {"at": 0.0, "stamp": None, "data": None}

# Returned by :func:`_shared_stamp` when the shared cache is unreachable: the snapshot
# then falls back to plain TTL expiry rather than failing the request.
_STAMP_UNAVAILABLE = object()


@dataclass(frozen=True)
class _CatalogueFit:
    """One active doctrine fit, flattened: what matching needs plus the raw column
    values needed to rebuild a real ``DoctrineFit`` on the way out."""

    ship_type_id: int
    module_types: frozenset[int]
    values: tuple


@dataclass(frozen=True)
class _CatalogueDoctrine:
    """One active doctrine and its fits, in the catalogue's canonical order."""

    values: tuple
    fits: tuple[_CatalogueFit, ...]


def _shared_stamp():
    """The current shared catalogue stamp, minting one when the cache has none.

    Minting on a miss is what makes the snapshot correct after any cache flush: a wiped
    stamp comes back as a NEW value, which no live snapshot can match, so every process
    reloads once. (The test suite clears the cache around every test, so this is also
    what stops one test's doctrines from being answered to the next.)
    """
    from django.core.cache import cache

    try:
        stamp = cache.get(_CATALOGUE_STAMP_KEY)
        if stamp is None:
            stamp = uuid.uuid4().hex
            cache.set(_CATALOGUE_STAMP_KEY, stamp, None)
        return stamp
    except Exception:  # noqa: BLE001 - a cache outage must not break doctrine matching
        return _STAMP_UNAVAILABLE


def _bump_shared_stamp() -> None:
    """Publish a new stamp so every other process drops its snapshot on its next match."""
    from django.core.cache import cache

    try:
        cache.set(_CATALOGUE_STAMP_KEY, uuid.uuid4().hex, None)
    except Exception:  # noqa: BLE001 - see _shared_stamp
        return


def _load_catalogue() -> dict:
    """Read the whole active catalogue once, in the canonical matching order.

    The query is character-for-character the one the uncached matchers used
    (``-priority``, ``name``, fits in ``DoctrineFit.Meta.ordering``), so the candidate
    sequence — and therefore every tie-break — is unchanged.
    """
    doctrine_fields = tuple(f.attname for f in Doctrine._meta.concrete_fields)
    fit_fields = tuple(f.attname for f in DoctrineFit._meta.concrete_fields)
    entries: list[_CatalogueDoctrine] = []
    for doctrine in (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits")
        .order_by("-priority", "name")
    ):
        entries.append(
            _CatalogueDoctrine(
                values=tuple(getattr(doctrine, name) for name in doctrine_fields),
                fits=tuple(
                    _CatalogueFit(
                        ship_type_id=fit.ship_type_id,
                        # Precomputed because the old code recomputed it inside the
                        # ``min`` key, once per candidate per call.
                        module_types=frozenset(_fit_module_types(fit.modules)),
                        values=tuple(getattr(fit, name) for name in fit_fields),
                    )
                    for fit in doctrine.fits.all()
                ),
            )
        )
    return {
        "entries": tuple(entries),
        "doctrine_fields": doctrine_fields,
        "fit_fields": fit_fields,
        "db": Doctrine.objects.db,
    }


def _catalogue() -> dict:
    """The current catalogue snapshot, rebuilding it when the stamp moved or it aged out."""
    stamp = _shared_stamp()

    def _fresh(snap: dict) -> bool:
        return (
            snap["data"] is not None
            and (time.monotonic() - snap["at"]) < _CATALOGUE_TTL
            and (stamp is _STAMP_UNAVAILABLE or stamp == snap["stamp"])
        )

    if _fresh(_CATALOGUE):
        return _CATALOGUE["data"]
    with _CATALOGUE_LOCK:
        # Another thread may have rebuilt it while we waited on the lock.
        if _fresh(_CATALOGUE):
            return _CATALOGUE["data"]
        data = _load_catalogue()
        _CATALOGUE.update(
            at=time.monotonic(),
            stamp=None if stamp is _STAMP_UNAVAILABLE else stamp,
            data=data,
        )
        return data


def reset_doctrine_catalogue() -> None:
    """Drop this process's catalogue snapshot and invalidate every other process's.

    Called automatically on any ``Doctrine`` / ``DoctrineFit`` save or delete. Call it
    by hand after a write that bypasses model signals — a ``queryset.update()`` that
    flips ``status``, a data migration, a raw SQL fix — if you need the new catalogue to
    be live before ``_CATALOGUE_TTL`` elapses.
    """
    _CATALOGUE.update(at=0.0, stamp=None, data=None)
    _publish_stamp_when_durable()


def _publish_stamp_when_durable() -> None:
    """Publish the new stamp only once the write it describes is actually committed.

    Bumping from inside an open transaction is worse than not bumping at all. The stamp
    lands in Redis immediately, but the row change is not yet visible to anyone else — so
    a concurrent worker that reloads *because* the stamp moved reads the PRE-commit
    database and then pins that stale catalogue under the POST-commit stamp. From then on
    the snapshot looks fresh to it, and it keeps matching against the old catalogue for the
    full ``_CATALOGUE_TTL`` after the officer's edit is live. SRP pays real ISK off that
    match, so the window matters even though it is short.

    The window is not theoretical: ``ModelAdmin.changeform_view`` wraps the whole admin
    save in ``transaction.atomic``, and ``doctrines.xml_import.commit_batch`` holds one
    open for a whole import. Deferring to ``on_commit`` closes it. The same pattern, for
    the same reason, is already used in ``apps.campaigns.signals``.

    Outside a transaction ``on_commit`` runs the callback inline, so management commands
    and shell fixes behave exactly as before. The local snapshot is cleared by the caller
    either way, so this process always sees its own writes immediately — including
    uncommitted ones, which is what a writer inside a transaction should see.
    """
    from django.db import transaction

    transaction.on_commit(_bump_shared_stamp)


def _materialise(catalogue: dict, entry: _CatalogueDoctrine, cfit: _CatalogueFit) -> DoctrineFit:
    """Build private ``DoctrineFit`` / ``Doctrine`` instances from snapshot values.

    ``from_db`` is the same constructor the ORM itself uses, so the result is
    indistinguishable from a freshly fetched row (``pk`` set, ``_state.adding`` false —
    it can be assigned straight to ``killmail.doctrine_fit``). The values are deep-copied
    because ``modules`` is a mutable JSON list: callers must never be able to reach into
    the shared snapshot.
    """
    doctrine = Doctrine.from_db(
        catalogue["db"], catalogue["doctrine_fields"], copy.deepcopy(entry.values)
    )
    fit = DoctrineFit.from_db(catalogue["db"], catalogue["fit_fields"], copy.deepcopy(cfit.values))
    fit.doctrine = doctrine
    return fit


def _candidates(catalogue: dict, ship_type_id: int) -> list[tuple[_CatalogueDoctrine, _CatalogueFit]]:
    """Every active fit for this hull, in priority-then-fit-id order."""
    return [
        (entry, cfit)
        for entry in catalogue["entries"]
        for cfit in entry.fits
        if cfit.ship_type_id == ship_type_id
    ]


def _best_from_catalogue(
    catalogue: dict, ship_type_id: int, fitted: dict[int, int] | None
) -> DoctrineFit | None:
    """The module-aware match against an already-loaded catalogue snapshot."""
    candidates = _candidates(catalogue, ship_type_id)
    if not candidates:
        return None
    if len(candidates) == 1 or not fitted:
        entry, cfit = candidates[0]
        return _materialise(catalogue, entry, cfit)

    fitted_types = set(fitted)
    # Distinct module types that differ (symmetric difference), quantity-insensitive.
    entry, cfit = min(candidates, key=lambda c: len(c[1].module_types ^ fitted_types))
    return _materialise(catalogue, entry, cfit)


def match_doctrine_fit(ship_type_id: int) -> DoctrineFit | None:
    """The DoctrineFit of the highest-priority active doctrine whose hull matches
    ``ship_type_id`` (or None). The returned fit has its ``.doctrine`` cached so
    callers can read it without an extra query. Shared by killboard doctrine
    tagging (KB-13) and SRP eligibility.

    Served from the shared catalogue snapshot (see above): repeated calls cost no
    queries, and the fit handed back is a private instance, safe to mutate.
    """
    catalogue = _catalogue()
    candidates = _candidates(catalogue, ship_type_id)
    if not candidates:
        return None
    entry, cfit = candidates[0]
    return _materialise(catalogue, entry, cfit)


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

    Backed by the shared catalogue snapshot, so N matches cost O(1) queries instead of
    2N. A caller that matches thousands of losses in one pass can bind the snapshot once
    with :func:`build_doctrine_matcher` and skip even the per-call stamp read.
    """
    return _best_from_catalogue(_catalogue(), ship_type_id, fitted)


def build_doctrine_matcher() -> Callable[[int, dict[int, int] | None], DoctrineFit | None]:
    """A drop-in ``best_doctrine_fit`` bound to ONE catalogue snapshot.

    Mirrors ``apps.market.pricing.build_price_index``: for a batch that matches many
    losses in a single pass (campaign analytics, a killboard re-tag backfill) this pins
    the catalogue for the whole batch, so the run is self-consistent and pays neither the
    per-call cache read nor a mid-batch reload. Resolution is identical to
    :func:`best_doctrine_fit`; the returned fits are private instances as usual. Use
    ``best_doctrine_fit`` for anything long-lived — a matcher held across requests would
    never see a doctrine edit.
    """
    catalogue = _catalogue()

    def match(ship_type_id: int, fitted: dict[int, int] | None = None) -> DoctrineFit | None:
        return _best_from_catalogue(catalogue, ship_type_id, fitted)

    return match


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


# --------------------------------------------------------------------------- #
#  Readiness status: a CODE, plus a display label
# --------------------------------------------------------------------------- #
# ``FitReadiness.status`` is a code, not prose. Every caller branches on it —
# ``{% if r.status == 'optimal' %}``, ``STATUS_RANK[status]``, the ``can_fly`` /
# sort-key helpers in ``browse``/``library`` — so translating the value itself would
# silently break readiness colouring, ranking and filtering for every non-English
# pilot. The code stays canonical English; only the *label* is translated, and only
# at render time.
READINESS_LABELS: dict[str, str] = {
    "optimal": _("optimal"),
    "viable": _("viable"),
    "not_ready": _("not ready"),
    "unknown": _("unknown"),
}


def readiness_label(code: str):
    """The human label for a readiness status code (the code itself if unmapped)."""
    return READINESS_LABELS.get(code, code)


@dataclass
class FitReadiness:
    fit_id: int
    fit_name: str
    status: str  # "optimal" | "viable" | "not_ready" | "unknown"
    missing_viable: list[dict] = field(default_factory=list)
    missing_optimal: list[dict] = field(default_factory=list)

    @property
    def status_label(self):
        """The translated label for ``status``. Read-time only — ``self.status`` stays
        the canonical code every ``==`` comparison is written against."""
        return readiness_label(self.status)


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
                    # ``status`` is the CODE the views/templates compare; ``status_label``
                    # is the half a human reads.
                    "status": best.status,
                    "status_label": readiness_label(best.status),
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


def doctrine_coverage(doctrine: Doctrine, characters, snapshots: dict | None = None) -> dict:
    """How many of the given characters can fly the doctrine (best fit).

    ``snapshots`` is the ``{character_id: latest CharacterSkillSnapshot}`` map produced by
    :func:`latest_snapshots`. Every caller of this function scores the SAME roster against
    MANY doctrines in a loop (the Operations board, the recommendation engine, Command
    Intel), and each snapshot row carries the pilot's whole skill sheet as TOASTed JSONB —
    so reloading the roster once per doctrine detoasts tens of megabytes per page for an
    answer that cannot have changed between iterations. Hoist it:

        snaps = latest_snapshots(characters)
        for doctrine in doctrines:
            counts = doctrine_coverage(doctrine, characters, snapshots=snaps)

    Omit it and the roster is loaded here exactly as before, so callers that match one
    doctrine keep working untouched. Either way the counts are identical — the argument
    only decides WHO issues the query, never what it returns.

    Fits and their skill requirements are likewise read from the doctrine's prefetch
    cache when the caller primed one (``prefetch_related("fits__skill_requirements")`` on
    the doctrine queryset) and fetched here otherwise, so a loop caller can drive the
    per-doctrine cost to zero queries without this function needing to know.
    """
    counts = {"optimal": 0, "viable": 0, "not_ready": 0, "unknown": 0}
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    snaps = latest_snapshots(characters) if snapshots is None else snapshots
    # Materialise the fits (with their skill requirements) ONCE, not per character —
    # otherwise doctrine.fits.all() + fit.skill_requirements re-query for every character,
    # and this runs per-doctrine per-op on the Operations list (the critical N+1).
    # ``fits.all()`` honours an existing prefetch cache (``doctrine.fits.prefetch_related``
    # would have discarded it and re-queried); ``prefetch_related_objects`` is a no-op when
    # the requirements are already loaded.
    fits = list(doctrine.fits.all())
    prefetch_related_objects(fits, "skill_requirements")
    for character in characters:
        best_status = "unknown"
        snapshot = snaps.get(character.character_id)
        for fit in fits:
            status = character_readiness(character, fit, snapshot=snapshot).status
            if rank[status] > rank[best_status]:
                best_status = status
        counts[best_status] += 1
    return counts


def latest_snapshots(characters) -> dict:
    """``{character_id: latest CharacterSkillSnapshot}`` in a single query.

    Public because it is the hoistable half of :func:`doctrine_coverage`: a caller
    scoring a roster against several doctrines loads this ONCE and threads the result
    through, instead of paying for the roster's whole skill JSONB per doctrine.
    """
    from apps.characters.models import CharacterSkillSnapshot

    return {
        s.character_id: s
        for s in CharacterSkillSnapshot.objects.filter(
            is_latest=True, character_id__in=[c.character_id for c in characters]
        )
    }


# Historical private name — kept so nothing that already imported it breaks.
_latest_snapshots = latest_snapshots


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

    snaps = latest_snapshots(characters)
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


# --------------------------------------------------------------------------- #
#  Catalogue invalidation
#
#  Wired here rather than in ``apps.py`` so the cache and the thing that invalidates it
#  stay in one file: a process that never imports this module has no snapshot to go
#  stale. Any ``Doctrine`` or ``DoctrineFit`` write — a status flip to/from ACTIVE, a
#  priority or name change (both are sort keys), an edited or deleted fit — republishes
#  the stamp, so the next match in EVERY process reloads. Cascade deletes are covered:
#  Django's collector emits ``post_delete`` per removed fit. ``SkillRequirement`` writes
#  are deliberately NOT wired: the catalogue holds only hull + module data, and coverage
#  reads requirements straight from the DB.
# --------------------------------------------------------------------------- #
def _invalidate_doctrine_catalogue(sender, **kwargs) -> None:
    reset_doctrine_catalogue()


for _signal in (post_save, post_delete):
    for _model in (Doctrine, DoctrineFit):
        _signal.connect(
            _invalidate_doctrine_catalogue,
            sender=_model,
            dispatch_uid="doctrines.services.catalogue-invalidate",
        )
