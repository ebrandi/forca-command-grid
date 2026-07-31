"""Audit P10/P11 — the two per-row query fan-outs in leadership reporting and buyback.

Both fixes are pure cost reductions, so every test here pairs a *budget* claim (the cost
stops scaling with the number of rows, or with the number of pasted lines) with a *result*
claim (the answer is exactly what the pre-fix code produced). The pre-fix implementations
are kept verbatim below as oracles, because the way these particular optimisations break is
silent rather than loud:

* a join-multiplied ``Count`` reports a track as "0 completed of 12" when the truth is
  "2 of 3" — leadership then chases a healthy track and ignores a dead one;
* a batch name resolution that picks a different row on a case clash values an appraisal
  at the wrong item's price, and the member is paid the wrong ISK.

**P10** ``apps.mentorship.reporting`` issued 2 COUNT queries per track, per active mentor
and per active pairing (roughly 150 on one admin console render). **P11**
``apps.buyback.appraisal`` ran 1-2 ``name__iexact`` scans of the ~52k-row ``SdeType`` table
per pasted line — and because ``parse_lines`` already strips, the second scan was usually a
byte-identical repeat, so every *unmatched* line paid for two full scans.

The fan-out fixtures deliberately give each parent MORE THAN ONE related row across MORE
THAN ONE relation (tracks get both enrolments and tasks, pairings get both assignments and
enrolments). That is the shape that exposes join multiplication: with one related row per
parent per relation a multiplied count is indistinguishable from a correct one.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.buyback.appraisal import appraise, parse_lines
from apps.market.models import MarketPrice
from apps.mentorship import reporting
from apps.mentorship.models import (
    MenteeProfile,
    MentorProfile,
    MentorshipEnrollment,
    MentorshipPairing,
    MentorshipTask,
    MentorshipTaskAssignment,
    MentorshipTaskValidation,
    MentorshipTrack,
)
from apps.sde.models import SdeCategory, SdeGroup, SdeType

pytestmark = pytest.mark.django_db

_P = MentorshipPairing.Status
_A = MentorshipTaskAssignment.Status
_E = MentorshipEnrollment.Status


def _cost(fn):
    """Run ``fn`` and return ``(result, queries_issued)``."""
    with CaptureQueriesContext(connection) as ctx:
        result = fn()
    return result, len(ctx.captured_queries)


# --------------------------------------------------------------------------- #
#  The pre-fix implementations, kept verbatim as oracles.
# --------------------------------------------------------------------------- #
def _oracle_track_completion_rates() -> list[dict]:
    """``reporting.track_completion_rates`` exactly as it read before the fix."""
    rows = []
    for track in MentorshipTrack.objects.filter(active=True).order_by("sort_order"):
        enrolled = track.enrollments.count()
        completed = track.enrollments.filter(status=_E.COMPLETED).count()
        rows.append({
            "track": track,
            "enrolled": enrolled,
            "completed": completed,
            "pct": int(round(100 * completed / enrolled)) if enrolled else 0,
        })
    return rows


def _oracle_mentor_activity(limit: int = 10) -> list[dict]:
    """``reporting.mentor_activity`` exactly as it read before the fix."""
    from django.db.models import Count

    signoffs = dict(
        MentorshipTaskValidation.objects.filter(
            source=MentorshipTaskValidation.Source.MENTOR,
            result=MentorshipTaskValidation.Result.PASS,
        ).values_list("actor").annotate(n=Count("id")).values_list("actor", "n")
    )
    rows = []
    for mentor in MentorProfile.objects.filter(
        status=MentorProfile.Status.ACTIVE
    ).select_related("user"):
        active = mentor.pairings.filter(status=_P.ACTIVE).count()
        completed = mentor.pairings.filter(status=_P.COMPLETED).count()
        rows.append({
            "mentor": mentor,
            "active_mentees": active,
            "completed": completed,
            "signoffs": signoffs.get(mentor.user_id, 0),
        })
    rows.sort(key=lambda r: (r["active_mentees"], r["signoffs"]), reverse=True)
    return rows[:limit]


def _oracle_mentees_needing_attention(limit: int = 10) -> list[dict]:
    """``reporting.mentees_needing_attention`` exactly as it read before the fix."""
    from django.utils.translation import gettext as _

    from apps.mentorship import matching

    out = [{"mentee": m, "why": _("Not yet paired")} for m in matching.unpaired_mentees()]
    for pairing in MentorshipPairing.objects.filter(status=_P.ACTIVE).select_related(
        "mentee__user", "mentor__user"
    ):
        done = pairing.assignments.filter(
            status__in=MentorshipTaskAssignment.DONE_STATUSES).count()
        total = pairing.assignments.count()
        if total and done == 0:
            out.append({"mentee": pairing.mentee, "why": _("Paired but no tasks completed yet"),
                        "pairing": pairing})
    return out[:limit]


def _oracle_resolved_type_ids(text: str) -> list[int | None]:
    """The pre-fix per-line type resolution from ``appraise``, verbatim.

    This is the *only* thing P11 changed, so pinning it line for line pins the whole
    behaviour of ``appraise``: everything downstream reads off the ``SdeType`` this picked.
    """
    out: list[int | None] = []
    for name, _qty in parse_lines(text):
        sde = (
            SdeType.objects.filter(name__iexact=name).select_related("group").first()
            or SdeType.objects.filter(name__iexact=name.strip()).select_related("group").first()
        )
        out.append(sde.type_id if sde else None)
    return out


def _observed_resolution(text: str, appraisal) -> list[int | None]:
    """Which type each parsed line resolved to, read back off a finished appraisal.

    ``appraise`` appends to ``lines`` and ``unknown`` in parse order, and ``parse_lines``
    guarantees each returned name is unique case-insensitively, so replaying the parse
    against the two output streams reconstructs the resolution — and any drift in the
    priced/unknown split shows up here too.
    """
    unknown = set(appraisal.unknown)
    priced = iter(appraisal.lines)
    return [None if name in unknown else next(priced).type_id for name, _qty in parse_lines(text)]


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #
def _mentor(dum, tag):
    return MentorProfile.objects.create(
        user=dum.objects.create(username=f"perf-mentor-{tag}"),
        status=MentorProfile.Status.ACTIVE,
    )


def _mentee(dum, tag):
    return MenteeProfile.objects.create(
        user=dum.objects.create(username=f"perf-mentee-{tag}"),
        status=MenteeProfile.Status.ACTIVE,
    )


def _track(tag, sort_order, *, tasks=2):
    """A track carrying tasks as well as enrolments — the multi-relation trap shape."""
    track = MentorshipTrack.objects.create(
        key=f"perf-track-{tag}", title=f"Perf track {tag}", sort_order=sort_order, active=True
    )
    for i in range(tasks):
        MentorshipTask.objects.create(track=track, key=f"perf-task-{tag}-{i}", title=f"T{i}")
    return track


@pytest.fixture
def only_perf_tracks(db):
    """Hide the seeded 12 tracks so ``sort_order`` alone gives a deterministic order.

    The seed migration's tracks are real data with their own sort orders; retiring them for
    the duration of a test keeps the oracle comparison an exact list-vs-list check instead
    of a set comparison that would not notice a reordering regression.
    """
    MentorshipTrack.objects.update(active=False)


def _mentorship_world(dum, tag, *, tracks, pairs_per_track=3):
    """``tracks`` tracks, each with ``pairs_per_track`` enrolments (one COMPLETED).

    Every pairing also carries three task assignments, so both the enrolment counts and the
    assignment counts run against a parent that has several rows in several relations.
    """
    made = [_track(f"{tag}{i}", sort_order=1000 + i) for i in range(tracks)]
    pairings = []
    for i in range(pairs_per_track):
        mentor = _mentor(dum, f"{tag}{i}")
        mentee = _mentee(dum, f"{tag}{i}")
        pairing = MentorshipPairing.objects.create(
            mentor=mentor, mentee=mentee, status=_P.ACTIVE)
        pairings.append(pairing)
        for track in made:
            MentorshipEnrollment.objects.create(
                pairing=pairing, track=track,
                status=_E.COMPLETED if i == 0 else _E.ACTIVE,
            )
            for task in track.tasks.all():
                MentorshipTaskAssignment.objects.create(
                    pairing=pairing, task=task,
                    # Pairing 0 has finished work; the rest have tasks but nothing done, so
                    # they are exactly the "needs attention" population.
                    status=_A.COMPLETED if i == 0 else _A.IN_PROGRESS,
                )
    return made, pairings


# --------------------------------------------------------------------------- #
#  P10 — track completion rates
# --------------------------------------------------------------------------- #
def test_track_completion_rates_cost_is_independent_of_track_count(
    django_user_model, only_perf_tracks
):
    """Adding tracks must not add queries — before the fix each one cost two more."""
    _mentorship_world(django_user_model, "a", tracks=2)
    small_rows, small_q = _cost(reporting.track_completion_rates)
    oracle_small_q = _cost(_oracle_track_completion_rates)[1]

    for i in range(6):
        _track(f"extra{i}", sort_order=2000 + i)
    big_rows, big_q = _cost(reporting.track_completion_rates)
    oracle_big_q = _cost(_oracle_track_completion_rates)[1]

    assert len(big_rows) == len(small_rows) + 6, "fixture did not actually grow the report"
    assert big_q == small_q, f"cost grew with track count: {small_q} -> {big_q}"
    assert big_q <= 2
    # The oracle is the pre-fix code: it fails the bound above, which is what makes this a
    # regression test rather than a description of whatever the code happens to do today.
    assert oracle_big_q == oracle_small_q + 12


def test_track_completion_rates_matches_pre_fix_counts(django_user_model, only_perf_tracks):
    """Three enrolments (one completed) and two tasks per track: a multiplied join would
    report 6 enrolled / 2 completed instead of 3 / 1."""
    _mentorship_world(django_user_model, "b", tracks=3, pairs_per_track=3)

    def _key(rows):
        return [(r["track"].pk, r["enrolled"], r["completed"], r["pct"]) for r in rows]

    assert _key(reporting.track_completion_rates()) == _key(_oracle_track_completion_rates())
    assert _key(reporting.track_completion_rates())[0][1:] == (3, 1, 33)


# --------------------------------------------------------------------------- #
#  P10 — mentor activity
# --------------------------------------------------------------------------- #
def _mentor_with_pairings(dum, tag, *, active, completed):
    mentor = _mentor(dum, tag)
    for i in range(active):
        MentorshipPairing.objects.create(
            mentor=mentor, mentee=_mentee(dum, f"{tag}-a{i}"), status=_P.ACTIVE)
    for i in range(completed):
        MentorshipPairing.objects.create(
            mentor=mentor, mentee=_mentee(dum, f"{tag}-c{i}"), status=_P.COMPLETED)
    # A pairing in a third state proves the conditional aggregates are actually filtering.
    MentorshipPairing.objects.create(
        mentor=mentor, mentee=_mentee(dum, f"{tag}-p"), status=_P.PAUSED)
    return mentor


def test_mentor_activity_cost_is_independent_of_mentor_count(django_user_model):
    _mentor_with_pairings(django_user_model, "m0", active=2, completed=3)
    _mentor_with_pairings(django_user_model, "m1", active=1, completed=1)
    small_rows, small_q = _cost(lambda: reporting.mentor_activity(limit=50))
    oracle_small_q = _cost(lambda: _oracle_mentor_activity(limit=50))[1]

    for i in range(2, 8):
        _mentor_with_pairings(django_user_model, f"m{i}", active=2, completed=2)
    big_rows, big_q = _cost(lambda: reporting.mentor_activity(limit=50))
    oracle_big_q = _cost(lambda: _oracle_mentor_activity(limit=50))[1]

    assert len(big_rows) == len(small_rows) + 6
    assert big_q == small_q, f"cost grew with mentor count: {small_q} -> {big_q}"
    assert big_q <= 3
    assert oracle_big_q == oracle_small_q + 12, "the pre-fix code paid 2 COUNTs per mentor"


def _activity_key(rows):
    return [(r["mentor"].pk, r["active_mentees"], r["completed"], r["signoffs"]) for r in rows]


def test_mentor_activity_matches_pre_fix_counts_and_order(django_user_model):
    """Several pairings per mentor in three different states, plus a mentor sign-off so the
    ranking tuple ``(active_mentees, signoffs)`` is exercised end to end.

    Four of the mentors deliberately TIE on that tuple. ``rows.sort`` is stable, so the
    queryset's own order decides a tie — and adding a GROUP BY makes Django discard
    ``Meta.ordering`` unless it is restated, which would reshuffle the leaderboard.
    """
    busy = _mentor_with_pairings(django_user_model, "busy", active=4, completed=2)
    _mentor_with_pairings(django_user_model, "quiet", active=1, completed=0)
    tied = [_mentor_with_pairings(django_user_model, f"tie{i}", active=2, completed=1)
            for i in range(4)]

    track = _track("signoff", sort_order=3000, tasks=1)
    pairing = busy.pairings.filter(status=_P.ACTIVE).first()
    assignment = MentorshipTaskAssignment.objects.create(
        pairing=pairing, task=track.tasks.get(), status=_A.COMPLETED)
    MentorshipTaskValidation.objects.create(
        assignment=assignment, source=MentorshipTaskValidation.Source.MENTOR,
        result=MentorshipTaskValidation.Result.PASS, actor=busy.user)

    rows = reporting.mentor_activity(limit=50)
    assert _activity_key(rows) == _activity_key(_oracle_mentor_activity(limit=50))
    assert _activity_key(rows)[0] == (busy.pk, 4, 2, 1)
    # Newest mentor first within the tied block, which is what ``Meta.ordering`` says.
    tied_pks = {m.pk for m in tied}
    assert [r["mentor"].pk for r in rows if r["mentor"].pk in tied_pks] == [
        m.pk for m in reversed(tied)
    ]


def test_mentor_activity_truncation_matches_pre_fix(django_user_model):
    """``limit`` cuts the ranked list, so a lost ordering silently swaps who is shown."""
    for i in range(5):
        _mentor_with_pairings(django_user_model, f"even{i}", active=2, completed=1)
    assert _activity_key(reporting.mentor_activity(limit=3)) == _activity_key(
        _oracle_mentor_activity(limit=3)
    )
    assert len(reporting.mentor_activity(limit=3)) == 3


# --------------------------------------------------------------------------- #
#  P10 — mentees needing attention
# --------------------------------------------------------------------------- #
def test_mentees_needing_attention_cost_is_independent_of_pairing_count(
    django_user_model, only_perf_tracks
):
    _mentorship_world(django_user_model, "c", tracks=1, pairs_per_track=2)
    small_rows, small_q = _cost(lambda: reporting.mentees_needing_attention(limit=50))
    oracle_small_q = _cost(lambda: _oracle_mentees_needing_attention(limit=50))[1]

    _mentorship_world(django_user_model, "d", tracks=1, pairs_per_track=7)
    big_rows, big_q = _cost(lambda: reporting.mentees_needing_attention(limit=50))
    oracle_big_q = _cost(lambda: _oracle_mentees_needing_attention(limit=50))[1]

    assert len(big_rows) > len(small_rows), "fixture did not add any flagged pairings"
    assert big_q == small_q, f"cost grew with pairing count: {small_q} -> {big_q}"
    assert big_q <= 4
    assert oracle_big_q == oracle_small_q + 14, "the pre-fix code paid 2 COUNTs per pairing"


def test_mentees_needing_attention_matches_pre_fix_selection(
    django_user_model, only_perf_tracks
):
    """Pairings with several assignments across several statuses, one pairing that has
    finished work, and one unpaired mentee — the full shape the report has to separate."""
    _mentorship_world(django_user_model, "e", tracks=2, pairs_per_track=3)
    loner = _mentee(django_user_model, "unpaired")

    def _key(rows):
        return [(r["mentee"].pk, str(r["why"]), r.get("pairing") and r["pairing"].pk)
                for r in rows]

    rows = reporting.mentees_needing_attention(limit=50)
    assert _key(rows) == _key(_oracle_mentees_needing_attention(limit=50))
    # The unpaired mentee leads; the two pairings with zero completions follow; the pairing
    # whose assignments are all COMPLETED is absent.
    assert _key(rows)[0][0] == loner.pk
    assert len(rows) == 3


# --------------------------------------------------------------------------- #
#  P11 — buyback appraisal name resolution
# --------------------------------------------------------------------------- #
@pytest.fixture
def sde_world(db):
    """Two priced minerals plus a deliberate case clash on one name.

    ``VELDSPAR`` is inserted BEFORE ``Veldspar`` and given the higher ``type_id``, so a
    batch resolution that keeps whichever row the database happens to hand back first would
    pick the wrong one. The pre-fix ``.first()`` on an unordered queryset ordered by ``pk``,
    which means the lowest ``type_id`` wins — and it is the row the appraisal must price.
    """
    material = SdeCategory.objects.create(category_id=4, name="Material")
    asteroid = SdeCategory.objects.create(category_id=25, name="Asteroid")
    minerals = SdeGroup.objects.create(group_id=18, category=material, name="Mineral")
    ores = SdeGroup.objects.create(group_id=450, category=asteroid, name="Veldspar")
    SdeType.objects.create(type_id=34, group=minerals, name="Tritanium", volume=0.01)
    SdeType.objects.create(type_id=35, group=minerals, name="Pyerite", volume=0.01)
    SdeType.objects.create(type_id=9999, group=ores, name="VELDSPAR", volume=0.1)
    SdeType.objects.create(type_id=1230, group=ores, name="Veldspar", volume=0.1)
    for type_id, price in [(34, "5.00"), (35, "10.00"), (1230, "15.00"), (9999, "999.00")]:
        MarketPrice.objects.create(
            type_id=type_id, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(price)
        )
    return True


def _appraise(text):
    return appraise(text, sec_band="highsec", rate=Decimal("0.90"))


def test_appraisal_lookup_cost_is_independent_of_line_count(sde_world):
    """The claim P11 makes: an all-unknown paste costs ONE resolution, not two per line.

    Before the fix a 30-line paste of unknown names ran 60 sequential scans of the type
    table against a 3-line paste's 6 — the growth this asserts away.
    """
    small_text = "\n".join(f"Nope{i} 1" for i in range(3))
    big_text = "\n".join(f"Nope{i} 1" for i in range(30))
    small, small_q = _cost(lambda: _appraise(small_text))
    big, big_q = _cost(lambda: _appraise(big_text))

    assert len(small.unknown) == 3 and len(big.unknown) == 30
    assert big_q == small_q, f"cost grew with line count: {small_q} -> {big_q}"
    assert big_q == 1, "an all-unknown paste should cost exactly one batch resolution"
    # The oracle is the pre-fix lookup: two scans per unmatched line, so it fails the bound.
    assert _cost(lambda: _oracle_resolved_type_ids(small_text))[1] == 6
    assert _cost(lambda: _oracle_resolved_type_ids(big_text))[1] == 60


def test_appraisal_lookup_cost_is_independent_of_line_count_with_known_items(sde_world):
    """Same claim with real items in the paste: only the (memoised) price lookups remain,
    and they are driven by the number of distinct priced types, never by the line count."""
    prefix = "Tritanium 100\nPyerite 50\n"
    small, small_q = _cost(lambda: _appraise(prefix + "\n".join(f"Junk{i} 1" for i in range(3))))
    big, big_q = _cost(lambda: _appraise(prefix + "\n".join(f"Junk{i} 1" for i in range(40))))

    assert small.offer_total == big.offer_total == Decimal("900.00")
    assert big_q == small_q, f"cost grew with line count: {small_q} -> {big_q}"


def test_appraisal_resolution_matches_pre_fix_lookup(sde_world):
    """A mixed paste — known, unknown, wrong-case, duplicate-merged — resolves to exactly
    the types the per-line ``iexact`` lookups picked, in exactly the same order."""
    text = (
        "Tritanium 100\n"
        "tRiTaNiUm 50\n"        # merges into the line above (case-insensitive dedupe)
        "PYERITE\t7\n"
        "Veldspar x2\n"
        "Definitely Not An Item 1\n"
        "Warp Scrambler II\n"
    )
    result = _appraise(text)
    assert _observed_resolution(text, result) == _oracle_resolved_type_ids(text)
    assert [line.type_id for line in result.lines] == [34, 35, 1230]
    assert [line.quantity for line in result.lines] == [150, 7, 2]
    assert result.unknown == ["Definitely Not An Item", "Warp Scrambler II"]


@pytest.mark.parametrize("spelling", ["Veldspar", "VELDSPAR", "veldspar", "vElDsPaR"])
def test_case_clash_resolves_to_the_same_row_as_before(sde_world, spelling):
    """Two types differing only by case: the lowest ``type_id`` wins for every spelling,
    which is what ``.first()`` on an unordered queryset did. Picking the other row would
    quietly value the line at 999 ISK instead of 15."""
    text = f"{spelling} 10"
    result = _appraise(text)
    assert _observed_resolution(text, result) == _oracle_resolved_type_ids(text)
    assert result.lines[0].type_id == 1230
    assert result.lines[0].name == "Veldspar"
    assert result.jita_total == Decimal("150.00")


def test_truncated_line_still_falls_back_to_the_stripped_name(sde_world):
    """The one case where the pre-fix second lookup was NOT a redundant repeat.

    ``parse_lines`` truncates to ``_MAX_LINE`` *after* stripping, so an over-long line can
    come back with trailing whitespace that the ``name__iexact=name.strip()`` fallback then
    rescued. The audit read that fallback as always byte-identical; it is not, so the batch
    resolver looks up both spellings and this pins the rescue.
    """
    text = "Tritanium" + " " * 195 + "x"
    parsed_name = parse_lines(text)[0][0]
    assert parsed_name != parsed_name.strip(), "fixture no longer exercises the fallback"

    with CaptureQueriesContext(connection) as ctx:
        result = _appraise(text)
    assert _observed_resolution(text, result) == _oracle_resolved_type_ids(text) == [34]
    assert result.lines[0].name == "Tritanium"
    type_reads = [q for q in ctx.captured_queries if 'FROM "sde_sdetype"' in q["sql"]]
    assert len(type_reads) == 1, "both spellings must ride the same batch query"


def test_empty_paste_touches_the_type_table_not_at_all(sde_world):
    """Nothing to resolve means no query — the early return in ``_resolve_types``."""
    result, queries = _cost(lambda: _appraise("   \n\n  \n"))
    assert result.lines == [] and result.unknown == []
    assert queries == 0


def test_ore_mode_still_reads_the_category_without_extra_queries(sde_world):
    """``select_related("group")`` survives the batch rewrite — the ore-mode category gate
    reads ``sde.group.category_id`` and must not become a query per line."""
    text = "\n".join(f"Veldspar{i} 1" for i in range(5)) + "\nTritanium 10\nVeldspar 10"
    with CaptureQueriesContext(connection) as ctx:
        result = appraise(text, sec_band="highsec", rate=Decimal("1.0"), ore_mode=True)
    type_reads = [q for q in ctx.captured_queries if 'FROM "sde_sdetype"' in q["sql"]]
    assert len(type_reads) == 1, "the type table is still being read more than once"
    assert "sde_sdegroup" in type_reads[0]["sql"], (
        "select_related('group') was lost — the ore-mode category gate would query per line"
    )
    assert [line.type_id for line in result.lines] == [34, 1230]
