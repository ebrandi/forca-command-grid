"""KB-34 D-scan / Local paste analyzer (WS-C4).

Two layers, tested separately:

* **Parsing** — pure functions, no DB/network. Fed real-shaped Local and D-scan pastes
  (multiple client shapes: with/without item ids, km + AU + '-' distances).
* **Analysis** — the DB/ESI layer. External calls (ESI name→id, affiliation, corp/alliance
  name fill) are injected as fakes so nothing hits the network; the counter-doctrine
  recommendation and the alert emission ride the REAL store/pingboard seams (stock via
  ``store.inventory.receive_stock``; the pingboard broadcast is asserted via a spy).

Home corp is 98000001 in test settings; engagement history uses the ``involves_home_corp`` /
``home_corp_role`` flags exactly like the adversary pages, so a threat read here reconciles
with them.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import scan_analyzer as scan
from apps.killboard.models import Killmail, KillmailParticipant, Watchlist, WatchlistEntry
from apps.sso.services import ensure_role

# SDE ship types we build for d-scan matching (type_id, name, group_id).
VEXOR, GUARDIAN, NAGLFAR, DAMNATION, RIFTER, MEGATHRON = 626, 11987, 19722, 22470, 587, 641
# Groups → (id, name, hull class per apps.doctrines.hulls)
GRP_CRUISER, GRP_LOGI, GRP_DREAD, GRP_COMMAND, GRP_FRIGATE, GRP_BATTLESHIP = 26, 832, 485, 540, 25, 27
SHIP_CATEGORY = 6

HOME = 98000001
ENEMY_A, ENEMY_B, NEUTRAL = 97000001, 97000002, 97000003
ENEMY_CORP, ENEMY_ALLIANCE = 98000002, 99000001


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #
def _member(django_user_model, username, role="member"):
    u = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.fixture
def ships(db):
    """A small, hand-built ship SDE (the bundled sample only has 2 hulls)."""
    from apps.sde.models import SdeCategory, SdeGroup, SdeType

    ship = SdeCategory.objects.create(category_id=SHIP_CATEGORY, name="Ship")
    drone = SdeCategory.objects.create(category_id=18, name="Drone")
    groups = {
        GRP_CRUISER: "Cruiser", GRP_LOGI: "Logistics", GRP_DREAD: "Dreadnought",
        GRP_COMMAND: "Command Ship", GRP_FRIGATE: "Frigate", GRP_BATTLESHIP: "Battleship",
    }
    for gid, name in groups.items():
        SdeGroup.objects.create(group_id=gid, category=ship, name=name)
    combat_drones = SdeGroup.objects.create(group_id=100, category=drone, name="Combat Drone")
    for tid, name, gid in (
        (VEXOR, "Vexor", GRP_CRUISER), (GUARDIAN, "Guardian", GRP_LOGI),
        (NAGLFAR, "Naglfar", GRP_DREAD), (DAMNATION, "Damnation", GRP_COMMAND),
        (RIFTER, "Rifter", GRP_FRIGATE), (MEGATHRON, "Megathron", GRP_BATTLESHIP),
    ):
        SdeType.objects.create(type_id=tid, group_id=gid, name=name)
    SdeType.objects.create(type_id=2456, group=combat_drones, name="Hobgoblin II")
    return True


def _km(kid, *, role, victim_char=None, victim_corp=None, value="10000000", npc=False):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=timezone.now() - dt.timedelta(days=1),
        solar_system_id=30000142, region_id=10000002, sec_band="highsec",
        victim_ship_type_id=RIFTER, victim_corporation_id=victim_corp or HOME,
        victim_character_id=victim_char, total_value=Decimal(value),
        involves_home_corp=True, home_corp_role=role, is_npc=npc,
    )


def _att(km, char, corp=ENEMY_CORP, ship=VEXOR):
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=1, character_id=char, corporation_id=corp, ship_type_id=ship,
    )


# =========================================================================== #
#  1. Parser — pure, no DB
# =========================================================================== #
def test_detect_kind_local_vs_dscan():
    assert scan.detect_kind("Ganker Bob\nScary Pilot\nNewbro Joe") == scan.ScanKind.LOCAL
    assert scan.detect_kind("123\tVexor\tCruiser\t14 km\n124\tRifter\tFrigate\t-") == scan.ScanKind.DSCAN
    # A single stray tab in a long name list must NOT flip it to d-scan.
    names = "\n".join(["Pilot One", "Pilot Two", "Pilot\tThree", "Pilot Four", "Pilot Five"])
    assert scan.detect_kind(names) == scan.ScanKind.LOCAL
    assert scan.detect_kind("") == scan.ScanKind.LOCAL


def test_parse_local_strips_dedups_and_drops_blanks():
    names = scan.parse_local("  Ganker Bob \n\nGanker Bob\nScary Pilot\n   \nGANKER BOB\n")
    assert names == ["Ganker Bob", "Scary Pilot"]  # case-insensitive dedupe, order preserved


def test_parse_dscan_multiple_client_shapes():
    # Shape A: itemID  name  group  distance (km / AU / -)
    paste = (
        "114235014\tVexor\tCruiser\t14 km\n"
        "114235015\tGuardian\tLogistics\t-\n"
        "114235016\tNaglfar\tDreadnought\t1.2 AU\n"
        "114235017\tRifter\tFrigate\t950 m\n"
    )
    rows = scan.parse_dscan(paste)
    assert len(rows) == 4
    kinds = {r["type_candidates"][0]: r["distance_kind"] for r in rows}
    assert kinds["Vexor"] == "on_grid"      # km
    assert kinds["Rifter"] == "on_grid"     # m
    assert kinds["Naglfar"] == "off_grid"   # AU
    assert kinds["Guardian"] == "off_grid"  # '-'
    # The itemID (all-digits) is dropped; the ship name survives as a candidate.
    assert all(not c.isdigit() for r in rows for c in r["type_candidates"])


def test_parse_dscan_without_ids_or_distance():
    # Shape B: just name + group, no id, no distance (older/short client copy).
    rows = scan.parse_dscan("Vexor\tCruiser\nDamnation\tCommand Ship")
    assert len(rows) == 2
    assert rows[0]["distance_kind"] is None
    assert "Vexor" in rows[0]["type_candidates"]


def test_parse_size_cap_rejects_oversize():
    with pytest.raises(scan.PasteTooLarge):
        scan.parse("x\n" * (scan.MAX_LINES + 5))
    with pytest.raises(scan.PasteTooLarge):
        scan.parse("A" * (scan.MAX_BYTES + 1))


def test_parse_routes_by_kind():
    assert scan.parse("Ann\nBob")["kind"] == scan.ScanKind.LOCAL
    assert scan.parse("1\tVexor\t14 km")["kind"] == scan.ScanKind.DSCAN
    assert scan.parse("Ann\nBob")["names"] == ["Ann", "Bob"]


# =========================================================================== #
#  2. Local analysis — injected ESI fakes
# =========================================================================== #
def _fake_resolver(mapping):
    def _fn(names):
        return {n.lower(): (cid, name) for n, (cid, name) in mapping.items()}
    return _fn


@pytest.mark.django_db
def test_analyze_local_affiliation_aggregation():
    resolver = _fake_resolver({
        "Ganker Bob": (ENEMY_A, "Ganker Bob"),
        "Scary Pilot": (ENEMY_B, "Scary Pilot"),
    })
    affil = lambda ids: {  # noqa: E731
        ENEMY_A: {"corporation_id": ENEMY_CORP, "alliance_id": ENEMY_ALLIANCE, "faction_id": None},
        ENEMY_B: {"corporation_id": ENEMY_CORP, "alliance_id": None, "faction_id": None},
    }
    names = {ENEMY_CORP: "Enemy Corp", ENEMY_ALLIANCE: "Enemy Alliance"}
    out = scan.analyze_local(
        ["Ganker Bob", "Scary Pilot"],
        char_resolver=resolver, affiliations_fn=affil, name_lookup_fn=lambda ids: names,
    )
    assert out["pilot_count"] == 2
    assert out["corporations"][0]["entity_id"] == ENEMY_CORP
    assert out["corporations"][0]["count"] == 2
    assert out["corporations"][0]["name"] == "Enemy Corp"
    assert out["alliances"][0]["entity_id"] == ENEMY_ALLIANCE
    assert out["alliances"][0]["count"] == 1


@pytest.mark.django_db
def test_analyze_local_unresolved_listed_honestly():
    resolver = _fake_resolver({"Ganker Bob": (ENEMY_A, "Ganker Bob")})
    out = scan.analyze_local(
        ["Ganker Bob", "Ghost Name", "Another Unknown"],
        char_resolver=resolver, affiliations_fn=lambda ids: {}, name_lookup_fn=lambda ids: {},
    )
    assert out["pilot_count"] == 1
    assert out["unresolved"] == ["Ghost Name", "Another Unknown"]


@pytest.mark.django_db
def test_analyze_local_watchlist_flags_and_sort():
    wl = Watchlist.objects.create(name="Known hostiles")
    WatchlistEntry.objects.create(watchlist=wl, entity_type="corporation", entity_id=ENEMY_CORP)
    resolver = _fake_resolver({
        "Ganker Bob": (ENEMY_A, "Ganker Bob"),
        "Neutral Guy": (NEUTRAL, "Neutral Guy"),
    })
    affil = lambda ids: {  # noqa: E731
        ENEMY_A: {"corporation_id": ENEMY_CORP, "alliance_id": None, "faction_id": None},
        NEUTRAL: {"corporation_id": 98000099, "alliance_id": None, "faction_id": None},
    }
    out = scan.analyze_local(
        ["Neutral Guy", "Ganker Bob"],
        char_resolver=resolver, affiliations_fn=affil, name_lookup_fn=lambda ids: {},
    )
    assert out["watched_count"] == 1
    # The watchlisted pilot (via corp) sorts first.
    assert out["threat"][0]["character_id"] == ENEMY_A
    assert out["threat"][0]["watched"] is True
    assert "Known hostiles" in out["threat"][0]["watchlists"]


@pytest.mark.django_db
def test_analyze_local_engagement_enrichment_danger():
    # ENEMY_A killed us three times and never died to us → Dangerous (ratio 1.0).
    for kid in (1, 2, 3):
        km = _km(kid, role=Killmail.HomeRole.VICTIM, victim_char=95000001)
        _att(km, ENEMY_A)
    # NEUTRAL we killed once, never lost to → Snuggly-ish (ratio 0).
    _km(4, role=Killmail.HomeRole.ATTACKER, victim_char=NEUTRAL, victim_corp=ENEMY_CORP)

    resolver = _fake_resolver({
        "Ganker Bob": (ENEMY_A, "Ganker Bob"),
        "Fresh Meat": (NEUTRAL, "Fresh Meat"),
    })
    out = scan.analyze_local(
        ["Fresh Meat", "Ganker Bob"],
        char_resolver=resolver, affiliations_fn=lambda ids: {}, name_lookup_fn=lambda ids: {},
    )
    by_id = {t["character_id"]: t for t in out["threat"]}
    assert by_id[ENEMY_A]["history"]["kills_vs_us"] == 3
    assert by_id[ENEMY_A]["history"]["losses_to_us"] == 0
    assert by_id[ENEMY_A]["history"]["danger"]["ratio"] == 1.0
    assert by_id[NEUTRAL]["history"]["losses_to_us"] == 1
    assert out["danger_count"] == 1  # only ENEMY_A is ratio >= 0.5
    # Most-dangerous sorts to the top (no watchlist entries here).
    assert out["threat"][0]["character_id"] == ENEMY_A


# =========================================================================== #
#  3. D-scan analysis
# =========================================================================== #
@pytest.mark.django_db
def test_analyze_dscan_composition_grid_and_notables(ships):
    paste = (
        "1\tVexor\tCruiser\t14 km\n"
        "2\tGuardian\tLogistics\t8 km\n"
        "3\tNaglfar\tDreadnought\t1.4 AU\n"
        "4\tDamnation\tCommand Ship\t-\n"
        "5\tRifter\tFrigate\t500 m\n"
        "6\tHobgoblin II\tCombat Drone\t2 km\n"      # a drone — must NOT count as a ship
        "7\tSome Mystery Structure\tStructure\t-\n"  # unmatched line
    )
    out = scan.analyze_dscan(scan.parse_dscan(paste))
    assert out["ship_count"] == 5           # drone + structure excluded
    assert out["unmatched"] == 2
    comp = {c["hull_class"]: c["count"] for c in out["composition"]}
    assert comp["Cruiser"] == 2             # Vexor + Guardian (logi folds into Cruiser)
    assert comp["Capital"] == 1
    assert comp["Battlecruiser"] == 1       # Damnation (Command Ship group)
    assert comp["Frigate"] == 1
    # Notable roles
    assert out["has_capital"] and out["has_logi"] and out["has_links"]
    assert out["notable"]["logi"][0]["name"] == "Guardian"
    assert out["notable"]["capital"][0]["name"] == "Naglfar"
    assert out["notable"]["links"][0]["name"] == "Damnation"
    # Grid split: km/m near, AU/'-' far
    assert out["on_grid"] == 3              # Vexor, Guardian, Rifter
    assert out["off_grid"] == 2             # Naglfar (AU), Damnation ('-')


@pytest.mark.django_db
def test_analyze_dscan_all_unmatched(ships):
    out = scan.analyze_dscan(scan.parse_dscan("9\tUnknown Thing\tJunk\t-"))
    assert out["ship_count"] == 0
    assert out["unmatched"] == 1
    assert out["composition"] == []


# =========================================================================== #
#  4. Counter-doctrine recommendation — real store stock seam
# =========================================================================== #
def _dscan_cruiser_led():
    return scan.analyze_dscan(scan.parse_dscan(
        "1\tVexor\tCruiser\t14 km\n2\tVexor\tCruiser\t9 km\n3\tNaglfar\tDreadnought\t2 AU"
    ))


@pytest.mark.django_db
def test_recommend_ranks_in_stock_doctrine_first(ships):
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.store import inventory as inv
    from apps.store.models import MarketLocation, ShipyardPolicy

    ShipyardPolicy.active()
    loc = MarketLocation.objects.create(
        name="Staging", location_type=MarketLocation.LocationType.STRUCTURE, system_id=30000142,
    )
    alpha = Doctrine.objects.create(name="Alpha Fleet", status=Doctrine.Status.ACTIVE, priority=5)
    mega = DoctrineFit.objects.create(doctrine=alpha, name="Megathron", ship_type_id=MEGATHRON, modules=[])
    cheap = Doctrine.objects.create(name="Cheap Tackle", status=Doctrine.Status.ACTIVE, priority=1)
    DoctrineFit.objects.create(doctrine=cheap, name="Rifter", ship_type_id=RIFTER, modules=[])
    inv.receive_stock(mega, location=loc, quantity=3, actor=None)  # 3 in stock

    recs = scan.recommend_counter_doctrines(_dscan_cruiser_led())
    assert recs["stock_configured"] is True
    assert recs["doctrines"][0]["name"] == "Alpha Fleet"
    assert recs["doctrines"][0]["has_stock"] is True
    assert recs["doctrines"][0]["total_atp"] == 3
    assert recs["doctrines"][0]["fits"][0]["atp"] == 3
    # 'Cheap Tackle' has no stock → ranks after the stocked doctrine.
    assert recs["doctrines"][1]["name"] == "Cheap Tackle"
    assert recs["doctrines"][1]["has_stock"] is False
    # Capitals on field + logi/links notes surfaced from the composition.
    assert "capitals_on_field" in recs["notes"]


@pytest.mark.django_db
def test_recommend_reports_stock_gap_honestly(ships):
    from apps.doctrines.models import Doctrine, DoctrineFit

    d = Doctrine.objects.create(name="Paper Fleet", status=Doctrine.Status.ACTIVE, priority=3)
    DoctrineFit.objects.create(doctrine=d, name="Megathron", ship_type_id=MEGATHRON, modules=[])

    recs = scan.recommend_counter_doctrines(_dscan_cruiser_led())
    assert recs["stock_configured"] is False   # no stock recorded → honest gap
    assert recs["doctrines"][0]["name"] == "Paper Fleet"
    assert recs["doctrines"][0]["total_atp"] == 0


@pytest.mark.django_db
def test_recommend_no_doctrines(ships):
    recs = scan.recommend_counter_doctrines(_dscan_cruiser_led())
    assert recs["doctrines"] == []
    assert recs["stock_configured"] is False


# =========================================================================== #
#  5. Alert summary + emission through pingboard
# =========================================================================== #
@pytest.mark.django_db
def test_build_alert_summary_shapes(ships):
    d = scan.analyze_dscan(scan.parse_dscan("1\tVexor\tCruiser\t14 km\n2\tNaglfar\tDreadnought\t2 AU"))
    line = scan.build_alert_summary(d, system="Tama")
    assert "Tama" in line and "2 ships" in line and "CAPITALS" in line


@pytest.mark.django_db
def test_emit_alert_calls_pingboard(ships, monkeypatch):
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("apps.pingboard.services.emit_broadcast", _spy)
    analysis = scan.analyze_dscan(scan.parse_dscan("1\tVexor\tCruiser\t14 km"))
    result = scan.emit_alert(analysis, system="Tama", source_id="scan:7")
    assert result is not None
    assert captured["source_service"] == "killboard"
    assert captured["audience"] == {"kind": "corp"}
    assert "Tama" in captured["body"] and "1 ships" in captured["body"]


# =========================================================================== #
#  6. Rate limit
# =========================================================================== #
@pytest.mark.django_db
def test_rate_limit_cooldown():
    uid = 424242
    oks = [scan.rate_limit_ok(uid) for _ in range(scan._RATE_LIMIT)]
    assert all(oks)                       # first N allowed
    assert scan.rate_limit_ok(uid) is False  # the (N+1)th is throttled


# =========================================================================== #
#  7. View — gating, analyze, alert, throttle
# =========================================================================== #
@pytest.mark.django_db
def test_scan_page_denied_to_anonymous(client):
    resp = client.get(reverse("killboard:scan"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_scan_page_denied_to_non_member(client, django_user_model):
    u = django_user_model.objects.create(username="nobody")
    client.force_login(u)
    resp = client.get(reverse("killboard:scan"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_scan_page_member_gets_form(client, django_user_model):
    client.force_login(_member(django_user_model, "scout"))
    resp = client.get(reverse("killboard:scan"))
    assert resp.status_code == 200
    assert b"D-scan" in resp.content


@pytest.mark.django_db
def test_scan_analyze_local_renders_threat_table(client, django_user_model, monkeypatch):
    client.force_login(_member(django_user_model, "scout2"))
    monkeypatch.setattr(scan, "_default_char_resolver",
                        _fake_resolver({"Ganker Bob": (ENEMY_A, "Ganker Bob")}))
    monkeypatch.setattr(scan, "_default_affiliations",
                        lambda ids: {ENEMY_A: {"corporation_id": ENEMY_CORP, "alliance_id": None}})
    monkeypatch.setattr(scan, "_default_name_lookup", lambda ids: {ENEMY_CORP: "Enemy Corp"})
    resp = client.post(reverse("killboard:scan"), {"paste": "Ganker Bob"})
    assert resp.status_code == 200
    assert b"Ganker Bob" in resp.content
    assert b"Enemy Corp" in resp.content


@pytest.mark.django_db
def test_scan_analyze_dscan_and_alert_emits(client, django_user_model, ships, monkeypatch):
    client.force_login(_member(django_user_model, "scout3"))
    captured = {}
    monkeypatch.setattr("apps.pingboard.services.emit_broadcast",
                        lambda **kw: captured.update(kw) or object())
    resp = client.post(reverse("killboard:scan"), {
        "paste": "1\tVexor\tCruiser\t14 km\n2\tNaglfar\tDreadnought\t2 AU",
        "system": "Tama", "send_alert": "1",
    })
    assert resp.status_code == 200
    assert b"Ship-class composition" in resp.content
    # The alert fired through the pingboard seam with our compact summary.
    assert captured.get("source_service") == "killboard"
    assert "Tama" in captured["body"]


@pytest.mark.django_db
def test_scan_dscan_recommendation_renders_with_stock(client, django_user_model, ships):
    """The unique kicker, end-to-end through the template: a d-scan paste yields a
    counter-doctrine card showing our in-stock count."""
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.store import inventory as inv
    from apps.store.models import MarketLocation, ShipyardPolicy

    ShipyardPolicy.active()
    loc = MarketLocation.objects.create(
        name="Staging", location_type=MarketLocation.LocationType.STRUCTURE, system_id=30000142,
    )
    d = Doctrine.objects.create(name="Alpha Fleet", status=Doctrine.Status.ACTIVE, priority=5)
    fit = DoctrineFit.objects.create(doctrine=d, name="Megathron", ship_type_id=MEGATHRON, modules=[])
    inv.receive_stock(fit, location=loc, quantity=4, actor=None)

    client.force_login(_member(django_user_model, "fc"))
    resp = client.post(reverse("killboard:scan"), {
        "paste": "1\tVexor\tCruiser\t14 km\n2\tNaglfar\tDreadnought\t2 AU",
    })
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Counter-doctrine suggestion" in body
    assert "Alpha Fleet" in body
    assert "4 in stock" in body
    assert "Capitals on field" in body  # the capitals note rendered from recommendations.notes


@pytest.mark.django_db
def test_scan_view_throttles(client, django_user_model):
    user = _member(django_user_model, "spammer")
    client.force_login(user)
    for _ in range(scan._RATE_LIMIT):
        scan.rate_limit_ok(user.id)  # exhaust the window
    resp = client.post(reverse("killboard:scan"), {"paste": "Somebody"})
    assert resp.status_code == 429
