"""KB-36 meta boards + battle-role detection (WS-D2).

Everything here is derived from OUR killmail history (home corp 98000001 in test settings) — the
matchup boards, hull performance and weapon board never make a universal claim, and role
detection reads a VICTIM's fitted modules (authoritative) or an ATTACKER's hull (approximation).
The synthetic histories below are hand-derived so every aggregation is pinned.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import meta, roles
from apps.killboard.models import (
    BattleReport,
    Killmail,
    KillmailItem,
    KillmailParticipant,
)
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role

HOME_CORP, ENEMY_CORP = 98000001, 98000002
# Ships (group in parens): Guardian=Logistics(832), Rifter=Frigate(25), Battleship=27,
# Dread=Dreadnought(883), FAX=Force Auxiliary(1538), Stabber/Vexor=Cruiser(26).
GUARDIAN, RIFTER, BATTLESHIP, DREAD, FAX, STABBER, VEXOR = 11987, 587, 24692, 19720, 37604, 622, 626
# Modules: remote rep=Remote Armor Repairer(325), scram=Warp Scrambler(52), ecm=ECM(201),
# burst=Command Burst(1698), autocannon=Projectile Weapon(55), neutron=Hybrid Weapon(509).
REMOTE_REP, SCRAM, ECM, BURST, AUTOCANNON, NEUTRON = 42889, 447, 1957, 42526, 484, 3178
AMMO = 185  # a loaded charge (category 8)
TAMA = 30002813

_VICTIM = Killmail.HomeRole.VICTIM
_ATTACKER = Killmail.HomeRole.ATTACKER

# Fitting-window slot flags (see fitrender.slot_bucket): high 27-34, med 19-26, low 11-18.
HI1, HI2, MED1, MED2, MED3 = 27, 28, 19, 20, 21


# --------------------------------------------------------------------------- #
#  SDE fixture — the ship/module groups the role + board maths resolve at runtime
# --------------------------------------------------------------------------- #
def _sde():
    from apps.sde.models import SdeCategory, SdeGroup, SdeType

    cats = {6: "Ship", 7: "Module", 8: "Charge"}
    for cid, name in cats.items():
        SdeCategory.objects.get_or_create(category_id=cid, defaults={"name": name})
    groups = {
        832: (6, "Logistics"), 25: (6, "Frigate"), 27: (6, "Battleship"),
        883: (6, "Dreadnought"), 1538: (6, "Force Auxiliary"), 26: (6, "Cruiser"),
        325: (7, "Remote Armor Repairer"), 52: (7, "Warp Scrambler"), 201: (7, "ECM"),
        1698: (7, "Command Burst"), 55: (7, "Projectile Weapon"), 509: (7, "Hybrid Weapon"),
        83: (8, "Projectile Ammo"),
    }
    for gid, (cat, name) in groups.items():
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": cat, "name": name})
    types = {
        GUARDIAN: 832, RIFTER: 25, BATTLESHIP: 27, DREAD: 883, FAX: 1538,
        STABBER: 26, VEXOR: 26,
        REMOTE_REP: 325, SCRAM: 52, ECM: 201, BURST: 1698, AUTOCANNON: 55, NEUTRON: 509,
        AMMO: 83,
    }
    for tid, gid in types.items():
        SdeType.objects.get_or_create(type_id=tid, defaults={"group_id": gid, "name": f"Type {tid}"})


def _km(kid, t, *, victim_ship, victim_corp, victim_char, value, role):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=t, solar_system_id=TAMA,
        victim_ship_type_id=victim_ship, victim_corporation_id=victim_corp,
        victim_character_id=victim_char, total_value=Decimal(value),
        involves_home_corp=True, home_corp_role=role,
    )


def _att(km, seq, char, corp, ship, weapon=None):
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
        ship_type_id=ship, weapon_type_id=weapon,
    )


def _vic(km, char, corp, ship):
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=char, corporation_id=corp, ship_type_id=ship,
    )


def _item(km, idx, type_id, flag):
    KillmailItem.objects.create(
        killmail=km, idx=idx, item_type_id=type_id, flag=flag, quantity_destroyed=1
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# --------------------------------------------------------------------------- #
#  Pure role classifiers (no DB — hull_class_for_group + group-name sets)
# --------------------------------------------------------------------------- #
def test_role_from_module_groups_and_precedence():
    assert roles.role_from_module_groups({"remote armor repairer"}) == roles.LOGI
    assert roles.role_from_module_groups({"warp scrambler"}) == roles.TACKLE
    assert roles.role_from_module_groups({"ecm"}) == roles.EWAR
    assert roles.role_from_module_groups({"command burst"}) == roles.LINKS
    assert roles.role_from_module_groups({"projectile weapon"}) is None
    # Precedence: logi outranks tackle when both are present.
    assert roles.role_from_module_groups({"warp scrambler", "remote armor repairer"}) == roles.LOGI


def test_victim_role_module_beats_hull_then_capital_then_dps():
    # A battleship hull (dps class) fitted with a remote rep is logi — the module wins.
    assert roles.victim_role({"remote armor repairer"}, 27) == roles.LOGI
    # A battleship with only guns is dps.
    assert roles.victim_role({"projectile weapon"}, 27) == roles.DPS
    # A dreadnought hull with no support module reads capital.
    assert roles.victim_role(set(), 883) == roles.CAPITAL
    # A triage-fit FAX (capital hull) with remote reps is logi, not capital (module wins).
    assert roles.victim_role({"remote shield booster"}, 1538) == roles.LOGI


def test_attacker_role_is_hull_approximation_with_documented_limits():
    # Logi hulls (by group name) and capital hulls are the only inferable roles.
    assert roles.attacker_role(832, "logistics") == roles.LOGI
    assert roles.attacker_role(1538, "force auxiliary") == roles.LOGI
    assert roles.attacker_role(883, "dreadnought") == roles.CAPITAL
    # Everything else defaults to dps — a scram-fit frigate attacker is NOT detectable.
    assert roles.attacker_role(27, "battleship") == roles.DPS
    assert roles.attacker_role(25, "frigate") == roles.DPS


# --------------------------------------------------------------------------- #
#  Battle-report composition ("2 logi vs their 1")
# --------------------------------------------------------------------------- #
def _report(kids):
    t = timezone.now() - dt.timedelta(minutes=10)
    r = BattleReport.objects.create(
        title="x", system_ids=[TAMA], start_time=t, end_time=timezone.now(),
        sides={}, ship_breakdown={},
    )
    r.killmails.set(kids)
    return r


@pytest.mark.django_db
def test_battle_role_composition_two_logi_vs_one():
    _sde()
    t = timezone.now() - dt.timedelta(minutes=5)
    P1, P2, P3, H_VIC, E1, E_VIC = 951, 952, 953, 954, 971, 972

    # km1: HOME kills an enemy frigate. Home attackers: 2 Guardians (logi hulls) + 1 battleship.
    km1 = _km(1, t, victim_ship=RIFTER, victim_corp=ENEMY_CORP, victim_char=E_VIC,
              value=8_000_000, role=_ATTACKER)
    _vic(km1, E_VIC, ENEMY_CORP, RIFTER)
    _att(km1, 1, P1, HOME_CORP, GUARDIAN)
    _att(km1, 2, P2, HOME_CORP, GUARDIAN)
    _att(km1, 3, P3, HOME_CORP, BATTLESHIP)
    # km2: ENEMY kills our frigate. Enemy attackers: 1 Guardian (logi) + the frigate (dps).
    km2 = _km(2, t, victim_ship=RIFTER, victim_corp=HOME_CORP, victim_char=H_VIC,
              value=10_000_000, role=_VICTIM)
    _vic(km2, H_VIC, HOME_CORP, RIFTER)
    _att(km2, 1, E1, ENEMY_CORP, GUARDIAN)
    _att(km2, 2, E_VIC, ENEMY_CORP, RIFTER)

    report = _report([1, 2])
    side_of_entity = {("corporation", HOME_CORP): 0, ("corporation", ENEMY_CORP): 1}
    comp = roles.battle_role_composition(report, side_of_entity)

    home = {r["role"]: r["count"] for r in comp[0]}
    enemy = {r["role"]: r["count"] for r in comp[1]}
    # P1 + P2 Guardians → 2 logi; P3 battleship + H_VIC frigate victim → 2 dps.
    assert home[roles.LOGI] == 2
    assert home[roles.DPS] == 2
    # E1 Guardian → 1 logi; E_VIC classified from its victim fit (frigate, no modules) → dps.
    assert enemy[roles.LOGI] == 1
    assert enemy[roles.DPS] == 1


@pytest.mark.django_db
def test_composition_victim_item_role_overrides_hull():
    """A pilot who died on a dps HULL but with remote reps fitted is logi (item-based wins)."""
    _sde()
    t = timezone.now() - dt.timedelta(minutes=5)
    H_VIC, E1 = 960, 973
    km = _km(1, t, victim_ship=BATTLESHIP, victim_corp=HOME_CORP, victim_char=H_VIC,
             value=300_000_000, role=_VICTIM)
    _vic(km, H_VIC, HOME_CORP, BATTLESHIP)
    _att(km, 1, E1, ENEMY_CORP, RIFTER)
    # The dead battleship was fitted with two remote armor reps (high slots) + a charge.
    _item(km, 0, REMOTE_REP, HI1)
    _item(km, 1, REMOTE_REP, HI2)
    _item(km, 2, AMMO, MED1)  # a charge — must be ignored (category 8)

    report = _report([1])
    comp = roles.battle_role_composition(report, {("corporation", HOME_CORP): 0,
                                                  ("corporation", ENEMY_CORP): 1})
    home = {r["role"]: r["count"] for r in comp[0]}
    assert home[roles.LOGI] == 1  # battleship hull, but item-based → logi


@pytest.mark.django_db
def test_composition_detects_each_module_role():
    """Each role module classifies its ship, over a dps hull, from the victim fit."""
    _sde()
    t = timezone.now() - dt.timedelta(minutes=5)
    fits = {
        981: (SCRAM, MED1, roles.TACKLE),
        982: (ECM, MED1, roles.EWAR),
        983: (BURST, MED1, roles.LINKS),
        984: (AUTOCANNON, HI1, roles.DPS),  # a gun → no role module → dps
    }
    kid = 1
    for char, (mod, flag, _expected) in fits.items():
        km = _km(kid, t, victim_ship=BATTLESHIP, victim_corp=HOME_CORP, victim_char=char,
                 value=100_000_000, role=_VICTIM)
        _vic(km, char, HOME_CORP, BATTLESHIP)
        _att(km, 1, 9999, ENEMY_CORP, RIFTER)
        _item(km, 0, mod, flag)
        kid += 1

    report = _report(list(range(1, kid)))
    comp = roles.battle_role_composition(report, {("corporation", HOME_CORP): 0,
                                                  ("corporation", ENEMY_CORP): 1})
    home = {r["role"]: r["count"] for r in comp[0]}
    assert home[roles.TACKLE] == 1
    assert home[roles.EWAR] == 1
    assert home[roles.LINKS] == 1
    assert home[roles.DPS] == 1


# --------------------------------------------------------------------------- #
#  Matchup boards — hand-derived history
# --------------------------------------------------------------------------- #
def _matchup_history():
    """Our Rifter losses + one Rifter kill, spread across time for the window tests.

    kmL1 (now-2d, 10M): lose a Rifter to a Stabber (autocannon) + a Vexor (neutron).
    kmL2 (now-60d, 12M): lose a Rifter to a Stabber (autocannon).
    kmL3 (now-120d, 5M): lose a Rifter to a Stabber (autocannon).
    kmK1 (now-1d, 8M): kill an enemy Rifter with TWO home battleships (neutron each).
    """
    now = timezone.now()
    kmL1 = _km(101, now - dt.timedelta(days=2), victim_ship=RIFTER, victim_corp=HOME_CORP,
               victim_char=501, value=10_000_000, role=_VICTIM)
    _vic(kmL1, 501, HOME_CORP, RIFTER)
    _att(kmL1, 1, 601, ENEMY_CORP, STABBER, weapon=AUTOCANNON)
    _att(kmL1, 2, 602, ENEMY_CORP, VEXOR, weapon=NEUTRON)

    kmL2 = _km(102, now - dt.timedelta(days=60), victim_ship=RIFTER, victim_corp=HOME_CORP,
               victim_char=502, value=12_000_000, role=_VICTIM)
    _vic(kmL2, 502, HOME_CORP, RIFTER)
    _att(kmL2, 1, 601, ENEMY_CORP, STABBER, weapon=AUTOCANNON)

    kmL3 = _km(103, now - dt.timedelta(days=120), victim_ship=RIFTER, victim_corp=HOME_CORP,
               victim_char=503, value=5_000_000, role=_VICTIM)
    _vic(kmL3, 503, HOME_CORP, RIFTER)
    _att(kmL3, 1, 601, ENEMY_CORP, STABBER, weapon=AUTOCANNON)

    kmK1 = _km(104, now - dt.timedelta(days=1), victim_ship=RIFTER, victim_corp=ENEMY_CORP,
               victim_char=604, value=8_000_000, role=_ATTACKER)
    _vic(kmK1, 604, ENEMY_CORP, RIFTER)
    _att(kmK1, 1, 511, HOME_CORP, BATTLESHIP, weapon=NEUTRON)
    _att(kmK1, 2, 512, HOME_CORP, BATTLESHIP, weapon=NEUTRON)


@pytest.mark.django_db
def test_what_kills_hull_top_ships_and_weapons():
    _sde()
    _matchup_history()
    m = meta.what_kills_hull(RIFTER, "all", use_cache=False)
    assert m["losses"]["count"] == 3
    ships = {r["ship_type_id"]: r["count"] for r in m["losses"]["top_ships"]}
    weapons = {r["weapon_type_id"]: r["count"] for r in m["losses"]["top_weapons"]}
    assert ships[STABBER] == 3 and ships[VEXOR] == 1
    assert weapons[AUTOCANNON] == 3 and weapons[NEUTRON] == 1
    # Inverse direction: what WE fly to kill their Rifter — two home battleships (participation).
    assert m["kills"]["count"] == 1
    kill_ships = {r["ship_type_id"]: r["count"] for r in m["kills"]["top_ships"]}
    assert kill_ships[BATTLESHIP] == 2


@pytest.mark.django_db
def test_what_kills_hull_window_filtering():
    _sde()
    _matchup_history()
    # 30d: only kmL1. 90d: kmL1 + kmL2. all: all three.
    assert meta.what_kills_hull(RIFTER, "30d", use_cache=False)["losses"]["count"] == 1
    assert meta.what_kills_hull(RIFTER, "90d", use_cache=False)["losses"]["count"] == 2
    assert meta.what_kills_hull(RIFTER, "all", use_cache=False)["losses"]["count"] == 3
    thirty = {r["ship_type_id"]: r["count"]
              for r in meta.what_kills_hull(RIFTER, "30d", use_cache=False)["losses"]["top_ships"]}
    assert thirty[STABBER] == 1 and thirty[VEXOR] == 1


@pytest.mark.django_db
def test_hull_performance_efficiency_and_kill_dedup():
    _sde()
    _matchup_history()
    perf = {r["ship_type_id"]: r for r in meta.hull_performance("all", use_cache=False)}
    # Battleship: on ONE kill (deduped across its two pilots), 8M destroyed, no losses → 100%.
    bs = perf[BATTLESHIP]
    assert bs["kills"] == 1
    assert bs["isk_destroyed"] == 8_000_000
    assert bs["losses"] == 0
    assert bs["efficiency"] == pytest.approx(100.0)
    # Rifter: three losses totalling 27M, no kills → 0%.
    rf = perf[RIFTER]
    assert rf["losses"] == 3
    assert rf["isk_lost"] == 27_000_000
    assert rf["kills"] == 0
    assert rf["efficiency"] == pytest.approx(0.0)


@pytest.mark.django_db
def test_weapon_board_counts_and_class_breakdown():
    _sde()
    _matchup_history()
    wb = meta.weapon_board("all", use_cache=False)
    weapons = {w["weapon_type_id"]: w for w in wb["weapons"]}
    # Our kill used two neutron blasters (participation count 2). The enemy's autocannons on our
    # LOSSES never appear — the weapon board is our kills only.
    assert weapons[NEUTRON]["count"] == 2
    assert AUTOCANNON not in weapons
    classes = {c["name"]: c["count"] for c in wb["classes"]}
    assert classes["Hybrid Weapon"] == 2


@pytest.mark.django_db
def test_hull_options_lists_seen_hulls_excluding_pods():
    _sde()
    _matchup_history()
    opts = {o["ship_type_id"] for o in meta.hull_options(use_cache=False)}
    # Rifter is a victim on both our losses and our kill; pods (670) never appear.
    assert RIFTER in opts
    assert 670 not in opts


@pytest.mark.django_db
def test_meta_insights_most_lost_hulls_with_top_killers():
    _sde()
    _matchup_history()
    ins = meta.meta_insights("all", use_cache=False)
    assert ins  # non-empty
    top = ins[0]
    assert top["ship_type_id"] == RIFTER  # our most-lost hull
    assert top["losses"] == 3
    killers = {k["ship_type_id"] for k in top["top_killers"]}
    assert STABBER in killers


# --------------------------------------------------------------------------- #
#  Caching sanity
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_board_is_cached_per_window():
    _sde()
    _matchup_history()
    # Warm the "all" cache, then add a fresh loss: the cached read is unchanged, the uncached
    # read reflects it — proving the (board, window) key memoises.
    first = meta.what_kills_hull(RIFTER, "all")["losses"]["count"]
    assert first == 3
    km = _km(105, timezone.now(), victim_ship=RIFTER, victim_corp=HOME_CORP,
             victim_char=505, value=1_000_000, role=_VICTIM)
    _vic(km, 505, HOME_CORP, RIFTER)
    _att(km, 1, 601, ENEMY_CORP, STABBER, weapon=AUTOCANNON)
    assert meta.what_kills_hull(RIFTER, "all")["losses"]["count"] == 3  # cached
    assert meta.what_kills_hull(RIFTER, "all", use_cache=False)["losses"]["count"] == 4  # live


def test_resolve_window_clamps_unknown_values():
    assert meta.resolve_window("90d") == "90d"
    assert meta.resolve_window("bogus") == meta.DEFAULT_WINDOW
    assert meta.resolve_window("") == meta.DEFAULT_WINDOW


# --------------------------------------------------------------------------- #
#  View gating (member + alliance-pilot audience, like the analytics dashboard)
# --------------------------------------------------------------------------- #
def _member(django_user_model, username, cid, role="member"):
    u = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=u, name=username,
                                is_main=True, is_corp_member=True)
    return u


def _plain(django_user_model, username):
    return django_user_model.objects.create(username=username)


@pytest.mark.django_db
def test_meta_page_denied_to_anonymous(client):
    resp = client.get(reverse("killboard:meta"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_meta_page_denied_to_non_member(client, django_user_model):
    client.force_login(_plain(django_user_model, "outsider"))
    resp = client.get(reverse("killboard:meta"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_member_sees_meta_page(client, django_user_model):
    _sde()
    _matchup_history()
    client.force_login(_member(django_user_model, "viewer", 40010))
    resp = client.get(reverse("killboard:meta"))
    assert resp.status_code == 200
    assert b"Combat Meta" in resp.content
    assert b"Hull performance" in resp.content


@pytest.mark.django_db
def test_member_selects_a_hull_matchup(client, django_user_model):
    _sde()
    _matchup_history()
    client.force_login(_member(django_user_model, "viewer2", 40011))
    resp = client.get(reverse("killboard:meta"), {"hull": RIFTER, "window": "all"})
    assert resp.status_code == 200
    assert b"What kills a" in resp.content
