"""Audit P4/P6/P8 — the active-doctrine catalogue snapshot and the hoistable coverage API.

Two pure cost reductions in ``apps.doctrines.services``, both of which must be provably
free of behaviour change:

* ``best_doctrine_fit`` / ``match_doctrine_fit`` used to re-materialise every active
  doctrine and fit on EVERY call, and both are called once per killmail from loops
  (campaign analytics, SRP eligibility, ingest tagging). They now read a process-local
  snapshot invalidated by a shared stamp.
* ``doctrine_coverage`` used to reload the whole roster's skill JSONB on every call, and
  every caller invokes it once per doctrine. It now accepts pre-loaded snapshots and
  honours an existing fits/requirements prefetch.

The tests below assert the two claims that matter: the query cost no longer scales with
the number of calls (or doctrines), and the ANSWER is byte-for-byte what the uncached
implementation produced — including multi-fit tie-breaking, which decides SRP payouts.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.doctrines.services import (
    _bump_shared_stamp,
    _fit_module_types,
    best_doctrine_fit,
    build_doctrine_matcher,
    doctrine_coverage,
    latest_snapshots,
    match_doctrine_fit,
    reset_doctrine_catalogue,
)
from apps.killboard.models import Killmail
from apps.sso.models import EveCharacter

pytestmark = pytest.mark.django_db

FEROX = 16227
DRAKE = 24698
RIFTER = 587
GUN_A = 2977   # blaster
GUN_B = 2969   # railgun
SHIELD = 2281
CARGO_AMMO = 12608


# --------------------------------------------------------------------------- #
#  The pre-fix implementation, kept verbatim as the oracle.
# --------------------------------------------------------------------------- #
def _uncached_best_doctrine_fit(ship_type_id, fitted):
    """``best_doctrine_fit`` exactly as it read before the snapshot landed.

    Any divergence between this and the cached function is a behaviour change, which for
    this code path means a mis-tagged loss and a mis-valued SRP claim.
    """
    candidates = []
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

    def _diff(fit):
        return len(_fit_module_types(fit.modules) ^ fitted_types)

    fit, doctrine = min(candidates, key=lambda c: _diff(c[0]))
    fit.doctrine = doctrine
    return fit


def _identity(fit):
    """Everything a caller can observe about a matched fit."""
    if fit is None:
        return None
    return (
        fit.id,
        fit.name,
        fit.ship_type_id,
        fit.modules,
        fit.doctrine_id,
        fit.doctrine.id,
        fit.doctrine.name,
        fit.doctrine.priority,
    )


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #
def _category():
    cat, _created = DoctrineCategory.objects.get_or_create(
        key="bc", defaults={"label": "Battlecruisers"}
    )
    return cat


def _catalogue():
    """Three same-hull Ferox variants (the tie-breaking case) plus a single-fit decoy."""
    cat = _category()
    blaster = DoctrineFit.objects.create(
        doctrine=Doctrine.objects.create(name="Ferox Blaster", category=cat, priority=90),
        name="Ferox (blaster)",
        ship_type_id=FEROX,
        modules=[{"type_id": GUN_A, "quantity": 7}, {"type_id": SHIELD, "quantity": 1}],
    )
    rail = DoctrineFit.objects.create(
        doctrine=Doctrine.objects.create(name="Ferox Rail", category=cat, priority=80),
        name="Ferox (rail)",
        ship_type_id=FEROX,
        modules=[
            {"type_id": GUN_B, "quantity": 7},
            {"type_id": CARGO_AMMO, "quantity": 5000, "slot": "cargo"},
        ],
    )
    # Same-hull third variant with the SAME module-type distance as the blaster fit for a
    # bare-hull loss — the tie that priority order has to break.
    twin = DoctrineFit.objects.create(
        doctrine=Doctrine.objects.create(name="Ferox Twin", category=cat, priority=70),
        name="Ferox (twin)",
        ship_type_id=FEROX,
        modules=[{"type_id": GUN_A, "quantity": 7}, {"type_id": SHIELD, "quantity": 1}],
    )
    solo = DoctrineFit.objects.create(
        doctrine=Doctrine.objects.create(name="Tackle", category=cat, priority=60),
        name="Rifter",
        ship_type_id=RIFTER,
        modules=[{"type_id": 484, "quantity": 2}],
    )
    return blaster, rail, twin, solo


def _more_doctrines(count: int):
    """Further active doctrines on unrelated hulls, so a test can prove the per-call cost
    does not grow with the catalogue."""
    cat = _category()
    for n in range(count):
        DoctrineFit.objects.create(
            doctrine=Doctrine.objects.create(name=f"Filler {n}", category=cat, priority=10 + n),
            name=f"Filler {n}",
            ship_type_id=DRAKE + n,
            modules=[{"type_id": GUN_A + n, "quantity": 3}],
        )


def _roster(size: int = 3):
    """``size`` corp pilots, each with a latest skill snapshot (the TOASTed JSONB blob
    ``doctrine_coverage`` used to reload once per doctrine)."""
    characters = []
    for n in range(size):
        char = EveCharacter.objects.create(
            character_id=7000 + n, name=f"Pilot {n}", is_corp_member=True
        )
        CharacterSkillSnapshot.objects.create(
            character=char,
            skills={"3331": {"trained_level": 5 if n else 1}, "3301": {"trained_level": 5}},
            is_latest=True,
        )
        characters.append(char)
    return characters


def _with_requirements(fit, **skills):
    for skill_type_id, level in skills.items():
        SkillRequirement.objects.create(
            fit=fit, skill_type_id=int(skill_type_id), min_level=level, optimal_level=level
        )
    return fit


# --------------------------------------------------------------------------- #
#  (a) the query cost no longer scales with the number of matches
# --------------------------------------------------------------------------- #
def test_repeated_matching_cost_is_independent_of_call_count():
    """5 matches and 50 matches must cost the SAME number of queries.

    Pre-fix this was two queries per call, so a large campaign board issued thousands.
    Both runs start from a cold snapshot so the load itself is inside the measurement.
    """
    _catalogue()

    reset_doctrine_catalogue()
    with CaptureQueriesContext(connection) as few:
        for _ in range(5):
            best_doctrine_fit(FEROX, {GUN_B: 7})

    reset_doctrine_catalogue()
    with CaptureQueriesContext(connection) as many:
        for _ in range(50):
            best_doctrine_fit(FEROX, {GUN_B: 7})

    assert len(many) == len(few)
    # And the whole cold load is a small constant, not a per-call cost.
    assert len(many) <= 4


def test_matching_cost_is_independent_of_catalogue_size():
    """Adding doctrines must not add per-match queries either."""
    _catalogue()
    for _ in range(20):
        best_doctrine_fit(FEROX, {GUN_B: 7})
    with CaptureQueriesContext(connection) as small:
        for _ in range(20):
            best_doctrine_fit(FEROX, {GUN_B: 7})

    _more_doctrines(6)  # the writes invalidate the snapshot via the model signals
    for _ in range(20):
        best_doctrine_fit(FEROX, {GUN_B: 7})
    with CaptureQueriesContext(connection) as large:
        for _ in range(20):
            best_doctrine_fit(FEROX, {GUN_B: 7})

    assert len(small) == len(large) == 0


def test_match_doctrine_fit_shares_the_snapshot():
    """The hull-only matcher is on the same hot paths and must be free too."""
    _catalogue()
    match_doctrine_fit(FEROX)
    with CaptureQueriesContext(connection) as cap:
        for _ in range(25):
            assert match_doctrine_fit(FEROX) is not None
    assert len(cap) == 0


def test_build_doctrine_matcher_binds_one_snapshot():
    """The batch matcher pins the catalogue for a whole pass and agrees with the oracle."""
    blaster, rail, _twin, _solo = _catalogue()
    match = build_doctrine_matcher()
    with CaptureQueriesContext(connection) as cap:
        for _ in range(30):
            assert match(FEROX, {GUN_B: 7}).id == rail.id
            assert match(FEROX, {GUN_A: 7, SHIELD: 1}).id == blaster.id
            assert match(99999, {GUN_A: 1}) is None
    assert len(cap) == 0


# --------------------------------------------------------------------------- #
#  (b) the answer is unchanged
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ship_type_id,fitted",
    [
        (FEROX, {GUN_B: 7}),                 # rail variant actually flown
        (FEROX, {GUN_A: 7, SHIELD: 1}),      # blaster variant actually flown
        (FEROX, {}),                         # no fitted data -> priority order
        (FEROX, None),                       # fitless killmail -> priority order
        (FEROX, {99999: 1}),                 # nothing in common -> tie, priority breaks it
        (RIFTER, {484: 2}),                  # single-fit hull
        (RIFTER, None),
        (DRAKE, {GUN_A: 1}),                 # no candidate at all
    ],
)
def test_cached_match_equals_uncached_match(ship_type_id, fitted):
    _catalogue()
    expected = _identity(_uncached_best_doctrine_fit(ship_type_id, fitted))
    # Twice: once cold, once served from the snapshot.
    reset_doctrine_catalogue()
    assert _identity(best_doctrine_fit(ship_type_id, fitted)) == expected
    assert _identity(best_doctrine_fit(ship_type_id, fitted)) == expected


def test_tie_between_same_hull_fits_still_breaks_on_priority():
    """Two candidates equidistant from the loss: the higher-priority doctrine wins.

    This is the case a caching bug would most easily flip — the snapshot must preserve
    the ``-priority, name`` candidate order that ``min`` relies on.
    """
    blaster, _rail, twin, _solo = _catalogue()
    assert blaster.modules == twin.modules  # identical distance for any fitted set
    fitted = {GUN_A: 7, SHIELD: 1}
    chosen = best_doctrine_fit(FEROX, fitted)
    assert chosen.id == blaster.id  # priority 90 beats the twin's 70
    assert chosen.doctrine.name == "Ferox Blaster"
    assert chosen.id == _uncached_best_doctrine_fit(FEROX, fitted).id


def test_returned_fit_is_a_private_instance():
    """Callers mutate what they get back (``fit.doctrine = ...``), so the snapshot must
    never hand out a shared object — one request's edit would leak into another's."""
    _catalogue()
    first = best_doctrine_fit(FEROX, {GUN_B: 7})
    original_modules = list(first.modules)
    first.name = "MUTATED"
    first.modules.append({"type_id": 1, "quantity": 1})
    first.doctrine.name = "MUTATED"

    second = best_doctrine_fit(FEROX, {GUN_B: 7})
    assert second is not first
    assert second.name == "Ferox (rail)"
    assert second.modules == original_modules
    assert second.doctrine.name == "Ferox Rail"
    # The mutation must not have changed matching either.
    assert best_doctrine_fit(FEROX, {GUN_B: 7}).id == second.id


def test_returned_fit_is_a_real_saved_row():
    """SRP and ingest assign the result straight to ``killmail.doctrine_fit``."""
    _blaster, rail, _twin, _solo = _catalogue()
    killmail = Killmail.objects.create(
        killmail_id=920001,
        killmail_time=timezone.now(),
        solar_system_id=30000142,
        victim_character_id=6001,
        victim_ship_type_id=FEROX,
        total_value=Decimal("50000000"),
        involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
    )
    fit = best_doctrine_fit(FEROX, {GUN_B: 7})
    assert fit.pk == rail.pk and fit._state.adding is False
    killmail.doctrine_fit = fit
    killmail.save(update_fields=["doctrine_fit"])
    killmail.refresh_from_db()
    assert killmail.doctrine_fit_id == rail.id


# --------------------------------------------------------------------------- #
#  (c) an officer's edit invalidates the snapshot
# --------------------------------------------------------------------------- #
def test_editing_a_fit_is_visible_immediately():
    blaster, rail, _twin, _solo = _catalogue()
    assert best_doctrine_fit(FEROX, {GUN_B: 7}).id == rail.id

    # Leadership re-arms the (higher priority) blaster doctrine with railguns: a railgun
    # loss now ties both fits, and priority moves the match — and therefore the SRP
    # valuation — to the blaster doctrine.
    blaster.modules = [{"type_id": GUN_B, "quantity": 7}]
    blaster.save()
    assert best_doctrine_fit(FEROX, {GUN_B: 7}).id == blaster.id
    assert best_doctrine_fit(FEROX, {GUN_B: 7}).id == _uncached_best_doctrine_fit(
        FEROX, {GUN_B: 7}
    ).id


def test_retiring_a_doctrine_is_visible_immediately():
    _blaster, _rail, _twin, solo = _catalogue()
    assert best_doctrine_fit(RIFTER, None).id == solo.id

    doctrine = solo.doctrine
    doctrine.status = Doctrine.Status.RETIRED
    doctrine.save()
    assert best_doctrine_fit(RIFTER, None) is None
    assert match_doctrine_fit(RIFTER) is None


def test_adding_and_deleting_a_fit_is_visible_immediately():
    _catalogue()
    assert best_doctrine_fit(DRAKE, None) is None

    drake = DoctrineFit.objects.create(
        doctrine=Doctrine.objects.get(name="Ferox Blaster"),
        name="Drake",
        ship_type_id=DRAKE,
        modules=[{"type_id": GUN_A, "quantity": 8}],
    )
    assert best_doctrine_fit(DRAKE, None).id == drake.id

    drake.delete()
    assert best_doctrine_fit(DRAKE, None) is None


def test_cascade_delete_of_a_doctrine_is_visible_immediately():
    _blaster, _rail, _twin, solo = _catalogue()
    assert best_doctrine_fit(RIFTER, None) is not None
    solo.doctrine.delete()  # cascades to the fit; post_delete fires per row
    assert best_doctrine_fit(RIFTER, None) is None


def test_another_process_edit_lands_via_the_shared_stamp():
    """A queryset ``update()`` fires no model signal, so it only reaches THIS process's
    snapshot once someone republishes the stamp — which is exactly what the worker that
    made the change would do. Simulating the stamp bump proves the cross-process seam
    works, and is also the documented escape hatch for signal-bypassing writes."""
    _blaster, _rail, _twin, solo = _catalogue()
    assert best_doctrine_fit(RIFTER, None).id == solo.id

    Doctrine.objects.filter(pk=solo.doctrine_id).update(status=Doctrine.Status.RETIRED)
    _bump_shared_stamp()
    assert best_doctrine_fit(RIFTER, None) is None


@pytest.mark.django_db(transaction=True)
def test_stamp_is_published_only_after_the_write_commits():
    """Publishing the stamp mid-transaction would pin the PRE-commit catalogue elsewhere.

    The stamp is not transactional — it goes straight to Redis — but the row change it
    announces is. Bump it while the transaction is still open and a concurrent worker
    reloads *because* the stamp moved, reads the database as it was BEFORE the edit, and
    stores those stale rows under the new stamp. From that moment its snapshot looks
    fresh, so it keeps matching against the old catalogue for the whole TTL after the
    edit went live — and SRP pays real ISK off that match.

    The window is reachable in normal use: ``ModelAdmin.changeform_view`` wraps the whole
    admin save in ``transaction.atomic``, and ``xml_import.commit_batch`` holds one open
    for an entire import. This test needs ``transaction=True`` because ``on_commit``
    callbacks never fire inside the rolled-back transaction the ordinary ``django_db``
    marker runs each test in.
    """
    from django.core.cache import cache
    from django.db import transaction

    from apps.doctrines.services import _CATALOGUE_STAMP_KEY, _shared_stamp

    doctrine = Doctrine.objects.create(
        name="Ferox Blaster", category=_category(), priority=90
    )
    before = _shared_stamp()

    with transaction.atomic():
        doctrine.priority = 10
        doctrine.save()
        assert cache.get(_CATALOGUE_STAMP_KEY) == before, (
            "the stamp was published while the edit was still uncommitted — another "
            "worker can now cache pre-commit rows and believe they are current"
        )

    assert cache.get(_CATALOGUE_STAMP_KEY) != before, (
        "the stamp never moved, so other workers keep their stale snapshot until the TTL"
    )


def test_stale_snapshot_cannot_outlive_a_cache_flush():
    """A cleared shared cache mints a fresh stamp, so no snapshot survives it.

    This is what keeps a warm snapshot from answering with another test's (rolled-back)
    doctrines, and in production what protects against a Redis flush leaving workers on
    a catalogue nobody can invalidate.
    """
    from django.core.cache import cache

    _catalogue()
    assert best_doctrine_fit(FEROX, {GUN_B: 7}) is not None

    Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).update(status=Doctrine.Status.RETIRED)
    cache.clear()
    assert best_doctrine_fit(FEROX, {GUN_B: 7}) is None


# --------------------------------------------------------------------------- #
#  (d) doctrine_coverage: hoisted snapshots change cost, not answers
# --------------------------------------------------------------------------- #
def test_coverage_with_hoisted_snapshots_matches_self_loading():
    blaster, rail, twin, solo = _catalogue()
    for fit in (blaster, rail, twin, solo):
        _with_requirements(fit, **{"3331": 5, "3301": 3})
    characters = _roster(3)

    doctrines = list(Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).order_by("id"))
    self_loading = [doctrine_coverage(d, characters) for d in doctrines]

    snaps = latest_snapshots(characters)
    hoisted = [doctrine_coverage(d, characters, snapshots=snaps) for d in doctrines]

    assert hoisted == self_loading
    # And the counts are the real answer, not two identically-broken ones.
    assert self_loading[0] == {"optimal": 2, "viable": 0, "not_ready": 1, "unknown": 0}


def test_coverage_accepts_snapshots_positionally_and_by_keyword():
    """Three call sites in other apps adopt this signature and cannot edit this module."""
    blaster, _rail, _twin, _solo = _catalogue()
    _with_requirements(blaster, **{"3331": 5})
    characters = _roster(2)
    snaps = latest_snapshots(characters)
    doctrine = blaster.doctrine
    assert (
        doctrine_coverage(doctrine, characters, snaps)
        == doctrine_coverage(doctrine, characters, snapshots=snaps)
        == doctrine_coverage(doctrine, characters)
    )


def test_coverage_loop_cost_is_independent_of_doctrine_count():
    """With snapshots hoisted and fits prefetched, a coverage loop costs a constant.

    Pre-fix each iteration issued a roster-wide snapshot query plus a fits query plus a
    requirements query, so the page cost grew linearly with the doctrine catalogue.
    """
    blaster, rail, twin, solo = _catalogue()
    for fit in (blaster, rail, twin, solo):
        _with_requirements(fit, **{"3331": 5})
    characters = _roster(3)

    def _loop_queries():
        with CaptureQueriesContext(connection) as cap:
            snaps = latest_snapshots(characters)
            doctrines = list(
                Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
                .prefetch_related("fits__skill_requirements")
            )
            rows = [doctrine_coverage(d, characters, snapshots=snaps) for d in doctrines]
        return len(cap), rows

    small, small_rows = _loop_queries()
    assert len(small_rows) == 4

    _more_doctrines(6)
    large, large_rows = _loop_queries()
    assert len(large_rows) == 10

    assert small == large
    # 1 snapshot query + doctrines + fits + requirements, whatever the catalogue size.
    assert large == 4


def test_coverage_reuses_a_primed_prefetch_cache():
    """A caller that prefetched fits+requirements must pay nothing more per doctrine."""
    blaster, rail, twin, solo = _catalogue()
    for fit in (blaster, rail, twin, solo):
        _with_requirements(fit, **{"3331": 5})
    characters = _roster(2)
    snaps = latest_snapshots(characters)
    doctrines = list(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements")
    )
    with CaptureQueriesContext(connection) as cap:
        for doctrine in doctrines:
            doctrine_coverage(doctrine, characters, snapshots=snaps)
    assert len(cap) == 0


def test_coverage_still_works_without_any_prefetch_or_snapshots():
    """The unhoisted path is the old behaviour, untouched."""
    blaster, _rail, _twin, _solo = _catalogue()
    _with_requirements(blaster, **{"3331": 5, "3301": 3})
    characters = _roster(3)
    doctrine = Doctrine.objects.get(pk=blaster.doctrine_id)
    assert doctrine_coverage(doctrine, characters) == {
        "optimal": 2,
        "viable": 0,
        "not_ready": 1,
        "unknown": 0,
    }


def test_coverage_reports_unknown_for_pilots_without_a_snapshot():
    """Hoisting must not turn a missing snapshot into a 'not ready' — the honest-data rule."""
    blaster, _rail, _twin, _solo = _catalogue()
    _with_requirements(blaster, **{"3331": 5})
    characters = _roster(2)
    characters.append(
        EveCharacter.objects.create(character_id=7999, name="Unimported", is_corp_member=True)
    )
    snaps = latest_snapshots(characters)
    counts = doctrine_coverage(blaster.doctrine, characters, snapshots=snaps)
    assert counts["unknown"] == 1
    assert counts == doctrine_coverage(blaster.doctrine, characters)
