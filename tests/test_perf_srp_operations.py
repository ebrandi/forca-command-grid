"""Audit P6/P8 — the loop callers of ``doctrine_coverage`` and of SRP ``eligibility``.

The expensive halves of both defects were fixed in ``apps.doctrines.services`` (the
catalogue snapshot and the hoistable ``doctrine_coverage`` signature). What is pinned here
is the other half: the *callers*, each of which invoked those authorities once per loop
iteration and paid a roster-wide skill-JSONB read, or a rule lookup, for an answer that
could not have changed between iterations.

* **P6** — ``operations`` (the ops list scores every visible op), ``recommendations``
  (the beat scores every active doctrine) and ``command_intel`` (the snapshot builder does
  the same) now load the roster's snapshots once and reuse them.
* **P8** — ``eligible_losses_for`` now scores its whole candidate window against one
  programme, one memoised SRP-rule lookup and one claims probe scoped to the candidates.

Every test therefore asserts two things: that the cost stops scaling with the loop
variable, and that the ANSWER is what the unhoisted path produced. SRP decides ISK payouts
and operation readiness decides whether a fleet is called — a cheaper number that is a
different number would be a far worse bug than the one being fixed.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.command_intel.engine.base import SnapshotContext
from apps.command_intel.sources.doctrine import DoctrineSource
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail
from apps.market.models import MarketPrice
from apps.operations import services as op_services
from apps.operations.models import Operation, OperationDoctrine
from apps.recommendations import engine as rec_engine
from apps.srp import services as srp_services
from apps.srp.models import SrpClaim, SrpRule
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

GUNNERY = 3300
RIFTER = 587
PUNISHER = 597
AUTOCANNON = 484


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _category() -> DoctrineCategory:
    cat, _created = DoctrineCategory.objects.get_or_create(key="dps", defaults={"label": "DPS"})
    return cat


def _doctrine(name: str, ship_type_id: int = RIFTER, *, priority: int = 80, level: int = 3):
    """An active doctrine with one fit and one derived skill requirement."""
    doctrine = Doctrine.objects.create(name=name, category=_category(), priority=priority)
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name=name, ship_type_id=ship_type_id,
        modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
    )
    SkillRequirement.objects.create(
        fit=fit, skill_type_id=GUNNERY, min_level=level, optimal_level=level
    )
    return doctrine


def _roster(django_user_model, size: int, *, first_id: int = 8100):
    """``size`` corp pilots with a latest skill snapshot each.

    The snapshot carries the pilot's whole skill sheet as JSONB — the payload every one of
    these call sites used to re-read once per doctrine, per operation.
    """
    characters = []
    for n in range(size):
        cid = first_id + n
        user = django_user_model.objects.create(username=f"eve:{cid}")
        char = EveCharacter.objects.create(
            character_id=cid, user=user, name=f"Pilot {cid}", is_main=True, is_corp_member=True
        )
        CharacterSkillSnapshot.objects.create(
            character=char, is_latest=True,
            skills={str(GUNNERY): {"trained_level": 5 if n % 2 else 1, "sp": 0}},
        )
        characters.append(char)
    return characters


def _snapshot_reads(captured) -> list[str]:
    """The captured queries that actually pull the roster's skill JSONB.

    Matched on the quoted ``skills`` COLUMN rather than the table, so the freshness
    aggregates (``MAX(as_of)``) that legitimately touch the same table are not counted —
    it is the detoasted blob, not the table, that this lane removes from the loops. (The
    table name itself contains the substring, hence the quotes.)
    """
    return [
        q["sql"] for q in captured
        if "characters_characterskillsnapshot" in q["sql"] and '."skills"' in q["sql"]
    ]


# --------------------------------------------------------------------------- #
#  P8 — SRP eligible-loss scanning
# --------------------------------------------------------------------------- #
def _srp_setup():
    """A doctrine, a matching SRP rule, and prices for the payout maths."""
    doctrine = _doctrine("Rifter Doctrine")
    MarketPrice.objects.create(
        type_id=RIFTER, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("500000")
    )
    MarketPrice.objects.create(
        type_id=AUTOCANNON, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("100000")
    )
    rule = SrpRule.objects.create(
        doctrine=None, basis=SrpRule.Basis.FIT, max_payout=0, active=True
    )
    return doctrine, rule


def _losses(count: int, *, character_id: int = 9001, first_id: int = 950000,
            ship_type_id: int = RIFTER):
    """``count`` corp losses by one pilot, newest first when read back."""
    now = timezone.now()
    made = []
    for n in range(count):
        made.append(Killmail.objects.create(
            killmail_id=first_id + n,
            killmail_time=now - timezone.timedelta(minutes=n),
            solar_system_id=30000142,
            victim_character_id=character_id,
            victim_ship_type_id=ship_type_id,
            total_value=Decimal("1000000"),
            destroyed_value=Decimal("900000"),
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.VICTIM,
        ))
    return made


def test_eligible_losses_cost_is_independent_of_the_candidate_count():
    """Scoring 20 candidate losses must cost the same queries as scoring 4.

    Pre-fix every loss re-ran the SRP-rule lookup (one or two queries) on top of the
    doctrine match, so the pilot's SRP page grew linearly with how many losses it scanned —
    and it scans ``limit * 3`` of them.
    """
    _srp_setup()
    _losses(4)
    char_ids = [9001]

    # Warm the process-local price and doctrine-catalogue snapshots so the measurement is
    # of the per-loss work, not of a one-off cold load landing in whichever run came first.
    srp_services.eligible_losses_for(char_ids, limit=25)
    with CaptureQueriesContext(connection) as few:
        rows_few = srp_services.eligible_losses_for(char_ids, limit=25)

    _losses(16, first_id=960000)
    srp_services.eligible_losses_for(char_ids, limit=25)
    with CaptureQueriesContext(connection) as many:
        rows_many = srp_services.eligible_losses_for(char_ids, limit=25)

    assert len(rows_few) == 4
    assert len(rows_many) == 20  # five times the work…
    assert len(many) == len(few)  # …for exactly the same number of queries


def test_eligible_losses_returns_what_per_loss_eligibility_returns():
    """The batch memo must not change a single verdict, payout or explanation."""
    _srp_setup()
    losses = _losses(6)
    rows = srp_services.eligible_losses_for([9001], limit=25)

    program = srp_services.active_program()
    expected = []
    for km in sorted(losses, key=lambda k: k.killmail_time, reverse=True):
        info = srp_services.eligibility(km, program)  # no batch: the unhoisted path
        if info.get("eligible"):
            expected.append((km.killmail_id, info["payout"], info["loss_value"],
                             info["insurance_estimate"], info["explanation"],
                             info["doctrine"].id, info["rule"].id))

    assert expected
    assert [
        (r["killmail"].killmail_id, r["payout"], r["loss_value"], r["insurance_estimate"],
         r["explanation"], r["doctrine"].id, r["rule"].id)
        for r in rows
    ] == expected


def test_the_claims_probe_is_scoped_to_the_candidate_window():
    """The 'already claimed?' probe must read the candidates, not the whole claim history.

    It used to pull every ``SrpClaim.killmail_id`` the corp had ever filed — unbounded and
    growing forever — to answer a membership test over at most ``limit * 3`` rows.
    """
    _srp_setup()
    losses = _losses(4)
    user = get_user_model().objects.create(username="claimant")
    SrpClaim.objects.create(
        killmail=losses[0], claimant=user, status=SrpClaim.Status.SUBMITTED,
        loss_value=Decimal("1"), computed_payout=Decimal("1"),
    )
    # A claim on a loss that is NOT in this pilot's candidate window.
    other = _losses(1, character_id=9099, first_id=970000)[0]
    SrpClaim.objects.create(
        killmail=other, claimant=user, status=SrpClaim.Status.SUBMITTED,
        loss_value=Decimal("1"), computed_payout=Decimal("1"),
    )

    srp_services.eligible_losses_for([9001], limit=25)  # warm the snapshots
    with CaptureQueriesContext(connection) as cap:
        rows = srp_services.eligible_losses_for([9001], limit=25)

    probes = [q["sql"] for q in cap.captured_queries if "srp_srpclaim" in q["sql"]]
    assert len(probes) == 1
    assert " IN (" in probes[0].upper(), "the claims probe must be bounded by the candidates"
    # Behaviour is what it always was: the claimed loss is hidden, the rest are offered.
    assert {r["killmail"].killmail_id for r in rows} == {k.killmail_id for k in losses[1:]}


def test_the_rule_memo_keeps_each_doctrine_on_its_own_rule():
    """A memo keyed per doctrine must never pay one doctrine's rule to another's loss.

    Two doctrines with different fixed-payout rules are scored in the same batch; if the
    memo collapsed them, one hull would silently be paid the other's amount.
    """
    _srp_setup()
    rifter = Doctrine.objects.get(name="Rifter Doctrine")
    punisher = _doctrine("Punisher Doctrine", PUNISHER, priority=70)
    SrpRule.objects.create(
        doctrine=rifter, basis=SrpRule.Basis.FIXED, max_payout=Decimal("1000000"), active=True
    )
    SrpRule.objects.create(
        doctrine=punisher, basis=SrpRule.Basis.FIXED, max_payout=Decimal("2000000"), active=True
    )
    _losses(2, first_id=980000, ship_type_id=RIFTER)
    _losses(2, first_id=980100, ship_type_id=PUNISHER)

    rows = srp_services.eligible_losses_for([9001], limit=25)
    payouts = {r["doctrine"].name: r["payout"] for r in rows}
    assert payouts == {
        "Rifter Doctrine": Decimal("1000000"),
        "Punisher Doctrine": Decimal("2000000"),
    }
    assert len(rows) == 4


def test_the_fleet_op_window_aggregate_runs_once_per_batch():
    """``loss_on_sanctioned_op``'s whole-table ``MAX(duration_minutes)`` is corp-wide.

    It cannot vary between the losses of one scan, yet it was recomputed for every loss the
    fleet-op gate examined. (The candidate-window query itself still runs per loss — it is
    keyed on that loss's timestamp — so only the aggregate is hoisted.)
    """
    _srp_setup()
    program = srp_services.active_program()
    program.require_fleet_op = True
    program.save()
    op = Operation.objects.create(
        name="Home Defence", type=Operation.Type.HOME_DEFENCE,
        status=Operation.Status.PLANNED, srp=Operation.Srp.CORP,
        target_at=timezone.now() - timezone.timedelta(minutes=10), duration_minutes=180,
    )
    assert op.pk
    _losses(8)

    srp_services.eligible_losses_for([9001], limit=25)  # warm
    with CaptureQueriesContext(connection) as cap:
        rows = srp_services.eligible_losses_for([9001], limit=25)

    aggregates = [
        q["sql"] for q in cap.captured_queries
        if "MAX(" in q["sql"].upper() and "duration_minutes" in q["sql"]
    ]
    assert len(aggregates) == 1
    assert len(rows) == 8  # every loss fell inside the sanctioned window, as before


def test_eligibility_without_a_batch_is_untouched():
    """The single-loss path — killboard detail, the stream filter, submit — is unchanged."""
    _srp_setup()
    km = _losses(1)[0]
    plain = srp_services.eligibility(km)
    batched = srp_services.eligibility(km, srp_services.active_program(),
                                       srp_services.EligibilityBatch())
    assert plain["eligible"] is True
    assert (plain["payout"], plain["explanation"], plain["rule"].id) == (
        batched["payout"], batched["explanation"], batched["rule"].id
    )


# --------------------------------------------------------------------------- #
#  P6 — operation readiness
# --------------------------------------------------------------------------- #
def _ops_with_doctrines(count: int, doctrines) -> list[Operation]:
    ops = []
    for n in range(count):
        op = Operation.objects.create(
            name=f"Op {n}", type=Operation.Type.HOME_DEFENCE, status=Operation.Status.PLANNED,
            target_at=timezone.now() + timezone.timedelta(days=1),
        )
        for doctrine in doctrines:
            OperationDoctrine.objects.create(operation=op, doctrine=doctrine, target_count=2)
        ops.append(op)
    return ops


def test_operation_readiness_is_identical_with_and_without_a_shared_roster(django_user_model):
    """The hoist decides who issues the query, never what the readiness says."""
    doctrines = [_doctrine("Core", level=3), _doctrine("Skirmish", level=5, priority=70)]
    _roster(django_user_model, 4)
    ops = _ops_with_doctrines(3, doctrines)

    standalone = [op_services.operation_readiness(op) for op in ops]

    roster = op_services.ReadinessRoster()
    shared = [op_services.operation_readiness(op, roster=roster) for op in ops]

    def _comparable(readiness):
        return (
            readiness["pct"], readiness["urgency"], readiness["at_risk_gaps"],
            [(r["doctrine"].id, r["ready"], r["known"], r["target"], r["pct"])
             for r in readiness["rows"]],
            [(g["doctrine_id"], g["label"], g["shortfall"], g["at_risk"])
             for g in readiness["gaps"]],
        )

    assert [_comparable(r) for r in shared] == [_comparable(r) for r in standalone]
    # And the numbers are the real answer, not two identically-broken ones: half the
    # roster has Gunnery V, so both doctrines are 2 ready of 4 known against a target of 2.
    assert standalone[0]["rows"][0]["ready"] == 2
    assert standalone[0]["rows"][0]["known"] == 4
    assert standalone[0]["pct"] == 100


def test_readiness_loop_cost_is_independent_of_the_operation_count(django_user_model):
    """Tripling the operations must not triple the readiness cost.

    Pre-fix each op reloaded the roster and, per doctrine, every pilot's skill JSONB — so
    the board's cost was ops × doctrines × roster. What is left per op is one small
    ``OperationDoctrine`` read (deliberately kept, see ``operation_readiness``), so the
    claim under test is that the expensive, roster-sized part is now a flat constant:
    six ops add four cheap queries, not four roster reloads.
    """
    doctrines = [_doctrine("Core", level=3), _doctrine("Skirmish", level=5, priority=70)]
    _roster(django_user_model, 5)

    def _score_all():
        with CaptureQueriesContext(connection) as cap:
            ops = Operation.objects.filter(status=Operation.Status.PLANNED)
            roster = op_services.ReadinessRoster()
            rows = [op_services.operation_readiness(op, roster=roster) for op in ops]
        return len(cap), cap.captured_queries, rows

    _ops_with_doctrines(2, doctrines)
    small, small_queries, small_rows = _score_all()
    assert len(small_rows) == 2

    _ops_with_doctrines(4, doctrines)
    large, large_queries, large_rows = _score_all()
    assert len(large_rows) == 6

    # Four more ops → four more OperationDoctrine reads and nothing else.
    assert large == small + 4
    assert len(_snapshot_reads(small_queries)) == len(_snapshot_reads(large_queries)) == 1
    assert [r["pct"] for r in large_rows] == [r["pct"] for r in small_rows] * 3


def test_readiness_scores_each_doctrine_once_however_many_ops_share_it(django_user_model):
    """Coverage is memoised per doctrine id: the roster's skills are read exactly once."""
    doctrines = [_doctrine("Core", level=3), _doctrine("Skirmish", level=5, priority=70)]
    _roster(django_user_model, 4)
    _ops_with_doctrines(5, doctrines)

    with CaptureQueriesContext(connection) as cap:
        roster = op_services.ReadinessRoster()
        for op in Operation.objects.filter(status=Operation.Status.PLANNED):
            op_services.operation_readiness(op, roster=roster)

    assert len(_snapshot_reads(cap.captured_queries)) == 1


def test_op_list_page_reads_the_roster_skills_once(client, django_user_model):
    """End to end: the ops board must not detoast the roster once per op × doctrine."""
    doctrines = [_doctrine("Core", level=3), _doctrine("Skirmish", level=5, priority=70)]
    _roster(django_user_model, 4)

    member = django_user_model.objects.create(username="member")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)

    _ops_with_doctrines(2, doctrines)
    with CaptureQueriesContext(connection) as small:
        assert client.get("/operations/").status_code == 200

    _ops_with_doctrines(4, doctrines)
    with CaptureQueriesContext(connection) as large:
        assert client.get("/operations/").status_code == 200

    assert len(_snapshot_reads(small.captured_queries)) == 1
    assert len(_snapshot_reads(large.captured_queries)) == 1


# --------------------------------------------------------------------------- #
#  P6 — the other two loop callers of doctrine_coverage
# --------------------------------------------------------------------------- #
def test_recommendation_sweep_reads_the_roster_skills_once(django_user_model):
    """The beat evaluates the whole active catalogue; the roster is one read for all of it."""
    for n in range(2):
        _doctrine(f"Doctrine {n}", RIFTER + n, priority=80 - n)
    _roster(django_user_model, 3)

    with CaptureQueriesContext(connection) as small:
        few = rec_engine.eval_doctrine_readiness()
    assert len(few) == 2

    for n in range(2, 8):
        _doctrine(f"Doctrine {n}", RIFTER + n, priority=80 - n)
    with CaptureQueriesContext(connection) as large:
        many = rec_engine.eval_doctrine_readiness()
    assert len(many) == 8

    assert len(_snapshot_reads(small.captured_queries)) == 1
    assert len(_snapshot_reads(large.captured_queries)) == 1
    # The drafts themselves are unchanged: half the roster can fly, one pilot cannot.
    assert few[0]["inputs"] == {"optimal": 1, "viable": 0, "not_ready": 2, "unknown": 0}
    assert [d["message"] for d in many[:2]] == [d["message"] for d in few]


def test_command_intel_doctrine_source_reads_the_roster_skills_once(django_user_model):
    """The Command Intel snapshot builder is the third loop over ``doctrine_coverage``."""
    for n in range(2):
        _doctrine(f"Intel {n}", RIFTER + n, priority=80 - n)
    characters = _roster(django_user_model, 3)
    source = DoctrineSource()

    with CaptureQueriesContext(connection) as small:
        few = source.collect(SnapshotContext(characters=characters))
    assert len(few.facts["doctrines"]) == 2

    for n in range(2, 8):
        _doctrine(f"Intel {n}", RIFTER + n, priority=80 - n)
    with CaptureQueriesContext(connection) as large:
        many = source.collect(SnapshotContext(characters=characters))
    assert len(many.facts["doctrines"]) == 8

    assert len(_snapshot_reads(small.captured_queries)) == 1
    assert len(_snapshot_reads(large.captured_queries)) == 1
    # Same slice, same numbers — only cheaper.
    assert many.facts["doctrines"][:2] == few.facts["doctrines"]
    assert few.facts["doctrines"][0]["flyable"] == 1
    assert few.facts["doctrines"][0]["not_ready"] == 2
