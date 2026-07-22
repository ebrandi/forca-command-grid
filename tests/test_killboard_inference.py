"""KB-27 character-intel inference (WS-D4).

Everything the classifier reads is OUR killmail history (home corp 98000001 in test settings):
playstyle from the attacker count on the mails a pilot was on, FC-likelihood from documented
fleet-lead signals, role usage via WS-D2 roles (victim fits exact, attacker hulls approximate),
awox from factual same-corp / flagged counts, and a sample-size confidence. Every synthetic
history below is hand-derived so each dimension is pinned, and every label the payload ships is
asserted against the counts behind it (the explainability rule).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import intel_inference as intel
from apps.killboard.models import Killmail, KillmailItem, KillmailParticipant
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role

HOME_CORP, ENEMY_CORP = 98000001, 98000002
# Ships (group in parens): Guardian=Logistics(832), Rifter=Frigate(25), Battleship=27,
# Stabber=Cruiser(26), Dread=Dreadnought(883).
GUARDIAN, RIFTER, BATTLESHIP, STABBER, DREAD = 11987, 587, 24692, 622, 19720
# Modules: remote rep=Remote Armor Repairer(325), autocannon=Projectile Weapon(55).
REMOTE_REP, AUTOCANNON = 42889, 484
AMMO = 185  # a loaded charge (category 8)
TAMA = 30002813
HI1, MED1 = 27, 19  # fitting-window slot flags

_VICTIM = Killmail.HomeRole.VICTIM
_ATTACKER = Killmail.HomeRole.ATTACKER


# --------------------------------------------------------------------------- #
#  SDE fixture — the ship/module groups the role maths resolves at runtime
# --------------------------------------------------------------------------- #
def _sde():
    from apps.sde.models import SdeCategory, SdeGroup, SdeType

    for cid, name in {6: "Ship", 7: "Module", 8: "Charge"}.items():
        SdeCategory.objects.get_or_create(category_id=cid, defaults={"name": name})
    groups = {
        832: (6, "Logistics"), 25: (6, "Frigate"), 27: (6, "Battleship"),
        26: (6, "Cruiser"), 883: (6, "Dreadnought"),
        325: (7, "Remote Armor Repairer"), 55: (7, "Projectile Weapon"),
        83: (8, "Projectile Ammo"),
    }
    for gid, (cat, name) in groups.items():
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": cat, "name": name})
    types = {
        GUARDIAN: 832, RIFTER: 25, BATTLESHIP: 27, STABBER: 26, DREAD: 883,
        REMOTE_REP: 325, AUTOCANNON: 55, AMMO: 83,
    }
    for tid, gid in types.items():
        SdeType.objects.get_or_create(type_id=tid, defaults={"group_id": gid, "name": f"Type {tid}"})


_KID = [1000]


def _next_kid() -> int:
    _KID[0] += 1
    return _KID[0]


def _kill(*, victim_char, victim_corp, victim_ship, attackers, when=None, value=1_000_000,
          is_awox=False):
    """A home KILL (we killed ``victim``). ``attackers``: list of (char, corp, ship, final_blow)."""
    kid = _next_kid()
    km = Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=when or timezone.now(),
        solar_system_id=TAMA, victim_ship_type_id=victim_ship, victim_corporation_id=victim_corp,
        victim_character_id=victim_char, total_value=Decimal(value),
        involves_home_corp=True, home_corp_role=_ATTACKER, is_awox=is_awox,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=victim_char,
        corporation_id=victim_corp, ship_type_id=victim_ship,
    )
    for seq, (char, corp, ship, fb) in enumerate(attackers, start=1):
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
            ship_type_id=ship, final_blow=fb,
        )
    return km


def _loss(*, victim_char, victim_corp, victim_ship, attackers, items=None, when=None,
          value=1_000_000, is_awox=False):
    """A home LOSS (we lost ``victim``). ``items``: list of (type_id, flag) fitted on the loss."""
    kid = _next_kid()
    km = Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=when or timezone.now(),
        solar_system_id=TAMA, victim_ship_type_id=victim_ship, victim_corporation_id=victim_corp,
        victim_character_id=victim_char, total_value=Decimal(value),
        involves_home_corp=True, home_corp_role=_VICTIM, is_awox=is_awox,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=victim_char,
        corporation_id=victim_corp, ship_type_id=victim_ship,
    )
    for seq, (char, corp, ship, fb) in enumerate(attackers, start=1):
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
            ship_type_id=ship, final_blow=fb,
        )
    for idx, (type_id, flag) in enumerate(items or []):
        KillmailItem.objects.create(
            killmail=km, idx=idx, item_type_id=type_id, flag=flag, quantity_destroyed=1
        )
    return km


# --------------------------------------------------------------------------- #
#  Playstyle bucketing (hand-derived)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_playstyle_buckets_by_total_attacker_count():
    _sde()
    P = 95001
    # 1 solo kill (P alone), 2 small-gang kills (3 attackers each), 1 fleet kill (7 attackers).
    _kill(victim_char=700, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
          attackers=[(P, HOME_CORP, RIFTER, True)])
    for _ in range(2):
        _kill(victim_char=701, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(P, HOME_CORP, RIFTER, False), (90, HOME_CORP, RIFTER, False),
                         (91, HOME_CORP, RIFTER, True)])
    _kill(victim_char=702, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
          attackers=[(P, HOME_CORP, RIFTER, False)] + [(80 + i, HOME_CORP, RIFTER, False)
                                                        for i in range(6)])

    ps = intel.character_intel(P, use_cache=False)["playstyle"]
    assert ps["buckets"] == {intel.SOLO: 1, intel.SMALL: 2, intel.FLEET: 1}
    assert ps["total"] == 4
    assert ps["dominant"] == intel.SMALL
    assert ps["shares"][intel.SOLO] == pytest.approx(0.25)
    assert ps["shares"][intel.SMALL] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
#  FC-likelihood — documented signals + explainability payload
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_fc_high_from_large_fleet_and_co_pilot_centrality():
    _sde()
    FC = 95010
    # 10 fleet kills, each FC + 6 DISTINCT co-pilots (unique across mails → 60 distinct co-pilots).
    for m in range(10):
        co = [(3000 + m * 6 + k, HOME_CORP, RIFTER, False) for k in range(6)]
        _kill(victim_char=710 + m, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(FC, HOME_CORP, RIFTER, False), *co])

    fc = intel.character_intel(FC, use_cache=False)["fc"]
    assert fc["level"] == intel.FC_HIGH
    keys = {s["key"] for s in fc["signals"]}
    assert keys == {"large_fleet_presence", "co_pilot_centrality"}
    # Every signal carries the counts behind it (explainability).
    by_key = {s["key"]: s["detail"] for s in fc["signals"]}
    assert by_key["large_fleet_presence"]["fleet_mails"] == 10
    assert by_key["co_pilot_centrality"]["distinct_co_pilots"] == 60
    assert fc["distinct_co_pilots"] == 60


@pytest.mark.django_db
def test_final_blow_rate_is_not_an_fc_signal():
    """A solo pilot with a 100% final-blow rate is NOT an FC — final blows are excluded by design."""
    _sde()
    SOLO = 95020
    for _ in range(12):  # n=12 → medium confidence, so FC isn't merely capped by a thin history
        _kill(victim_char=720, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(SOLO, HOME_CORP, RIFTER, True)])  # always the final blow

    prof = intel.character_intel(SOLO, use_cache=False)
    assert prof["confidence"]["level"] == intel.CONF_MEDIUM
    assert prof["fc"]["level"] == intel.FC_LOW
    assert prof["fc"]["signals"] == []
    assert prof["playstyle"]["dominant"] == intel.SOLO


@pytest.mark.django_db
def test_fc_capped_low_when_confidence_low():
    """Even a wide co-pilot spread reads FC low with too little history (leadership isn't inferred
    from a handful of mails)."""
    _sde()
    FC = 95011
    # 3 fleet mails, many distinct co-pilots → centrality would fire, but n=3 → low confidence.
    for m in range(3):
        co = [(4000 + m * 6 + k, HOME_CORP, RIFTER, False) for k in range(6)]
        _kill(victim_char=730 + m, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(FC, HOME_CORP, RIFTER, False), *co])
    prof = intel.character_intel(FC, use_cache=False)
    assert prof["confidence"]["level"] == intel.CONF_LOW
    assert prof["fc"]["level"] == intel.FC_LOW


# --------------------------------------------------------------------------- #
#  Role usage — victim fits exact, attacker hulls approximate (WS-D2)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_role_shares_victim_exact_and_attacker_approx():
    _sde()
    R = 95030
    # Attacker in a Guardian (logi hull approx) on 2 mails; attacker in a Rifter (dps) on 1.
    for _ in range(2):
        _kill(victim_char=740, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(R, HOME_CORP, GUARDIAN, True)])
    _kill(victim_char=741, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
          attackers=[(R, HOME_CORP, RIFTER, True)])
    # Loss in a BATTLESHIP fitted with a remote rep → logi (item-based beats the dps hull).
    _loss(victim_char=R, victim_corp=HOME_CORP, victim_ship=BATTLESHIP,
          attackers=[(999, ENEMY_CORP, STABBER, True)],
          items=[(REMOTE_REP, HI1), (AMMO, MED1)])  # the charge must be ignored
    # Loss in a Rifter with only a gun → dps.
    _loss(victim_char=R, victim_corp=HOME_CORP, victim_ship=RIFTER,
          attackers=[(999, ENEMY_CORP, STABBER, True)], items=[(AUTOCANNON, HI1)])

    roles = intel.character_intel(R, use_cache=False)["roles"]
    # logi = 2 attacker Guardians + 1 battleship-loss (item exact); dps = 1 attacker Rifter + 1 Rifter loss.
    assert roles["counts"]["logi"] == 3
    assert roles["counts"]["dps"] == 2
    assert roles["total"] == 5
    assert roles["shares"]["logi"] == pytest.approx(0.6)
    assert roles["dominant"] == "logi"
    # ``ordered`` lists non-zero roles with their counts (the explainable payload).
    ordered = {r["role"]: r["count"] for r in roles["ordered"]}
    assert ordered == {"logi": 3, "dps": 2}


# --------------------------------------------------------------------------- #
#  Awox — factual counts (same-corp victim + is_awox flagged), never a score
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_awox_same_corp_victim_and_flagged_counts():
    _sde()
    AWOX = 95040
    early = timezone.now() - dt.timedelta(days=3)
    late = timezone.now() - dt.timedelta(days=1)
    # Shot a corpmate: AWOX (HOME) attacks a HOME victim → same-corp event AND an is_awox mail.
    _loss(victim_char=800, victim_corp=HOME_CORP, victim_ship=RIFTER,
          attackers=[(AWOX, HOME_CORP, RIFTER, True)], when=early, is_awox=True)
    # An is_awox mail where AWOX is on the kill but flying under a DIFFERENT corp than the victim
    # → flagged only, not a same-corp event.
    _loss(victim_char=801, victim_corp=HOME_CORP, victim_ship=RIFTER,
          attackers=[(AWOX, ENEMY_CORP, RIFTER, True)], when=late, is_awox=True)

    awox = intel.character_intel(AWOX, use_cache=False)["awox"]
    assert awox["events"] == 1      # only the same-corp mail
    assert awox["flagged"] == 2     # both is_awox mails AWOX was on
    assert awox["has_risk"] is True
    assert awox["last"] is not None


@pytest.mark.django_db
def test_no_awox_is_quiet():
    _sde()
    P = 95041
    _kill(victim_char=810, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
          attackers=[(P, HOME_CORP, RIFTER, True)])
    awox = intel.character_intel(P, use_cache=False)["awox"]
    assert awox == {"events": 0, "flagged": 0, "last": None, "has_risk": False}


# --------------------------------------------------------------------------- #
#  Confidence tiers by sample size
# --------------------------------------------------------------------------- #
def test_confidence_tiers_boundaries():
    assert intel.confidence_for(0) == intel.CONF_LOW
    assert intel.confidence_for(9) == intel.CONF_LOW
    assert intel.confidence_for(10) == intel.CONF_MEDIUM
    assert intel.confidence_for(49) == intel.CONF_MEDIUM
    assert intel.confidence_for(50) == intel.CONF_HIGH


@pytest.mark.django_db
def test_confidence_reflects_engagement_count():
    _sde()
    P = 95050
    for _ in range(3):  # 3 engagements → low
        _kill(victim_char=820, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(P, HOME_CORP, RIFTER, True)])
    prof = intel.character_intel(P, use_cache=False)
    assert prof["engagements"] == 3
    assert prof["confidence"]["level"] == intel.CONF_LOW
    assert prof["confidence"]["n"] == 3


# --------------------------------------------------------------------------- #
#  Empty history — honest unknown, low confidence, not an error
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_empty_history_is_unknown_low_confidence():
    _sde()
    prof = intel.character_intel(91234567, use_cache=False)
    assert prof["has_history"] is False
    assert prof["engagements"] == 0
    assert prof["confidence"]["level"] == intel.CONF_LOW
    assert prof["playstyle"]["dominant"] is None
    assert prof["fc"]["level"] == intel.FC_LOW
    assert prof["roles"]["ordered"] == []
    assert prof["awox"]["has_risk"] is False


@pytest.mark.django_db
def test_playstyle_gaps_lists_missing_buckets():
    _sde()
    P = 95060
    # Only fleet kills → solo and small-gang are gaps a cadet could grow into.
    for m in range(6):
        co = [(5000 + m * 6 + k, HOME_CORP, RIFTER, False) for k in range(6)]
        _kill(victim_char=830 + m, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(P, HOME_CORP, RIFTER, False), *co])
    prof = intel.character_intel(P, use_cache=False)
    gaps = {g["code"] for g in intel.playstyle_gaps(prof)}
    assert gaps == {intel.SOLO, intel.SMALL}
    # A pilot with no kills at all has no basis for a gap.
    assert intel.playstyle_gaps(intel.character_intel(91234567, use_cache=False)) == []


# --------------------------------------------------------------------------- #
#  Recruitment vetting panel — the inference block is surfaced with confidence
# --------------------------------------------------------------------------- #
def _officer(dum, username="rec-officer"):
    u = dum.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role("officer"))
    return u


@pytest.mark.django_db
def test_recruitment_detail_renders_inference_block(client, django_user_model, sde):
    from apps.recruitment.models import Candidate

    _sde()
    CAND = 95070
    # Enough history to have a profile with a clear role read.
    for _ in range(3):
        _kill(victim_char=840, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(CAND, HOME_CORP, GUARDIAN, True)])
    candidate = Candidate.objects.create(character_id=CAND, name="Applicant")
    client.force_login(_officer(django_user_model))
    resp = client.get(reverse("recruitment:detail", args=[candidate.pk]))
    assert resp.status_code == 200
    assert b"Inferred combat profile" in resp.content
    assert b"confidence" in resp.content  # the sample-size caption is always shown


@pytest.mark.django_db
def test_recruitment_intel_is_none_without_history(django_user_model):
    from apps.recruitment import services

    _sde()
    assert services.killboard_intel(96000001) is None  # never seen on our board


# --------------------------------------------------------------------------- #
#  Mentorship matching hint — cadet gaps + mentor strengths in the worklist
# --------------------------------------------------------------------------- #
def _member(dum, username, cid):
    u = dum.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role("member"))
    EveCharacter.objects.create(character_id=cid, user=u, name=username,
                                is_main=True, is_corp_member=True)
    return u


@pytest.mark.django_db
def test_mentorship_matching_shows_combat_hint(client, django_user_model, sde):
    from apps.mentorship.models import MenteeProfile, MentorProfile

    _sde()
    # Cadet: only fleet kills (no solo/small-gang → a solo gap).
    CADET_CID, MENTOR_CID = 95080, 95081
    cadet_user = _member(django_user_model, "cadet-fleet", CADET_CID)
    for m in range(6):
        co = [(6000 + m * 6 + k, HOME_CORP, RIFTER, False) for k in range(6)]
        _kill(victim_char=850 + m, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(CADET_CID, HOME_CORP, RIFTER, False), *co])
    # Mentor: solo kills.
    mentor_user = _member(django_user_model, "mentor-solo", MENTOR_CID)
    for _ in range(3):
        _kill(victim_char=860, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
              attackers=[(MENTOR_CID, HOME_CORP, RIFTER, True)])

    MenteeProfile.objects.create(user=cadet_user, status=MenteeProfile.Status.ACTIVE, goals=["pvp"])
    MentorProfile.objects.create(user=mentor_user, status=MentorProfile.Status.ACTIVE, areas=["pvp"])

    officer = _officer(django_user_model, "mtch-officer")
    RoleAssignment.objects.get_or_create(user=officer, role=ensure_role("officer"))
    client.force_login(officer)
    resp = client.get(reverse("admin_audit:mentorship_matching"))
    assert resp.status_code == 200
    assert b"Killboard read:" in resp.content  # the hint panel rendered
    assert b"mostly" in resp.content           # the cadet's dominant playstyle read


# --------------------------------------------------------------------------- #
#  Adversary character page — shares the classifier + cache
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_adversary_character_page_shows_inference(client, django_user_model, sde):
    _sde()
    ENEMY = 95090
    # The enemy attacked us (our losses) — enough for a profile and an inference block.
    for _ in range(3):
        _loss(victim_char=870, victim_corp=HOME_CORP, victim_ship=RIFTER,
              attackers=[(ENEMY, ENEMY_CORP, STABBER, True)])
    client.force_login(_member(django_user_model, "kb-viewer", 41001))
    resp = client.get(reverse("killboard:adversary_character", args=[ENEMY]))
    assert resp.status_code == 200
    assert b"Inferred combat profile" in resp.content


# --------------------------------------------------------------------------- #
#  Query budget — a full cold build stays within the documented ceiling
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_intel_build_query_budget(django_assert_max_num_queries):
    """A full build (attacker mails + losses + items + awox + centrality) is a fixed, small set of
    indexed aggregates — independent of history size. ``QUERY_CEILING`` is the documented bound."""
    _sde()
    P = 95100
    # Exercise every path: attacker mails (2 hulls), losses with items, and a co-pilot spread.
    _kill(victim_char=880, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
          attackers=[(P, HOME_CORP, GUARDIAN, False), (70, HOME_CORP, RIFTER, True)])
    _kill(victim_char=881, victim_corp=ENEMY_CORP, victim_ship=RIFTER,
          attackers=[(P, HOME_CORP, RIFTER, True)])
    _loss(victim_char=P, victim_corp=HOME_CORP, victim_ship=BATTLESHIP,
          attackers=[(999, ENEMY_CORP, STABBER, True)], items=[(REMOTE_REP, HI1)])

    with django_assert_max_num_queries(intel.QUERY_CEILING):
        prof = intel.character_intel(P, use_cache=False)
        assert prof["has_history"] and prof["roles"]["ordered"]
