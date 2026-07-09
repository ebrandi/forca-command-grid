"""Combat stats dashboard: analytics aggregation, access control, and rendering."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import analytics
from apps.killboard.models import Killmail, KillmailParticipant
from apps.sso.services import ensure_role
from core import rbac

HOME = 98000001
HOME_ALLIANCE = 99000001
ENEMY = 55555


def _km(km_id, *, role, value="100000000", victim_ship=587, is_npc=False,
        is_solo=False, when=None, system=30000142, region=10000002, victim_char=None,
        sec_band="nullsec"):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}",
        killmail_time=when or timezone.now(), solar_system_id=system, region_id=region,
        victim_ship_type_id=victim_ship, total_value=Decimal(value), points=1,
        is_solo=is_solo, is_npc=is_npc, involves_home_corp=True, home_corp_role=role,
        victim_character_id=victim_char, sec_band=sec_band,
        victim_corporation_id=HOME if role == Killmail.HomeRole.VICTIM else ENEMY,
    )
    # A home attacker row so PvP kills look real (not strictly needed for these rollups).
    if role == Killmail.HomeRole.ATTACKER:
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=1, character_id=1001,
            corporation_id=HOME, ship_type_id=22456, final_blow=True, damage_done=100,
        )
    return km


@pytest.fixture
def combat(db, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    # 3 kills (two Rifters, one Punisher) + 1 loss + 1 NPC ratting death (ignored).
    _km(1, role=Killmail.HomeRole.ATTACKER, value="100000000", victim_ship=587, is_solo=True)
    _km(2, role=Killmail.HomeRole.ATTACKER, value="100000000", victim_ship=587)
    _km(3, role=Killmail.HomeRole.ATTACKER, value="300000000", victim_ship=597)
    _km(4, role=Killmail.HomeRole.VICTIM, value="200000000", victim_ship=22456)
    _km(5, role=Killmail.HomeRole.VICTIM, value="999000000", victim_ship=22456, is_npc=True)
    return None


# --- Aggregation ------------------------------------------------------------
@pytest.mark.django_db
def test_summary_efficiency_and_counts(combat):
    s = analytics.summary()
    assert s["kills"] == 3 and s["losses"] == 1  # NPC death excluded
    assert s["solo_kills"] == 1
    assert s["isk_destroyed"] == 500_000_000 and s["isk_lost"] == 200_000_000
    # 500M / (500M + 200M) ≈ 71.4%
    assert round(s["efficiency"], 1) == 71.4
    assert s["danger"]["label"] == "Risky"  # 3 kills / 1 loss = 75% → Risky band


@pytest.mark.django_db
def test_monthly_series_lands_in_current_bucket(combat):
    m = analytics.monthly_series(months=12)
    assert len(m["labels"]) == 12 and len(m["kills"]) == 12
    # All fixture mails are "now", so they fall in the last (current) bucket.
    assert m["kills"][-1] == 3 and m["losses"][-1] == 1
    assert m["isk_destroyed"][-1] == 500_000_000 and m["isk_lost"][-1] == 200_000_000
    assert sum(m["kills"][:-1]) == 0  # nothing in prior months


@pytest.mark.django_db
def test_top_ships_killed_and_lost(combat):
    ships = analytics.top_ships(limit=10)
    killed = {r["ship_type_id"]: r["count"] for r in ships["killed"]}
    assert killed == {587: 2, 597: 1}  # two Rifters, one Punisher destroyed
    lost = {r["ship_type_id"]: r["count"] for r in ships["lost"]}
    assert lost == {22456: 1}  # only the PvP loss; NPC death excluded


@pytest.mark.django_db
def test_activity_heatmap_places_fights_in_utc_cells(combat):
    h = analytics.activity_heatmap(days=90)
    assert len(h["rows"]) == 7 and all(len(r) == 24 for r in h["rows"])
    now = timezone.now()
    # 3 kills + 1 loss = 4 PvP fights, all "now" → same UTC cell.
    assert h["rows"][now.isoweekday() - 1][now.hour] == 4
    assert h["peak"] == 4 and h["total"] == 4


# --- Tier 2: ship-class, doctrine, rollup, per-pilot ------------------------
@pytest.mark.django_db
def test_ship_class_breakdown_totals(combat):
    sc = analytics.ship_class_breakdown()
    # Whatever the SDE group names, class counts sum to the kill/loss totals.
    assert sum(r["count"] for r in sc["killed"]) == 3
    assert sum(r["count"] for r in sc["lost"]) == 1


@pytest.mark.django_db
def test_doctrine_compliance(combat):
    from apps.doctrines.models import Doctrine, DoctrineFit

    # No active doctrine → not configured (UI hides the chart).
    assert analytics.doctrine_compliance()["configured"] is False
    d = Doctrine.objects.create(name="Shield Cruisers", status=Doctrine.Status.ACTIVE)
    DoctrineFit.objects.create(doctrine=d, name="Fit", ship_type_id=22456)  # the fielded hull
    dc = analytics.doctrine_compliance()
    assert dc["configured"] is True
    assert dc["on"] == 3 and dc["off"] == 0 and dc["total"] == 3
    assert round(dc["on_pct"]) == 100


@pytest.mark.django_db
def test_rebuild_corp_metrics_enriched(combat):
    from apps.killboard.models import CombatMetric
    from apps.killboard.stats import rebuild_corp_metrics

    rebuild_corp_metrics()
    m = CombatMetric.objects.get(entity_type="corporation", entity_id=HOME, window="all")
    assert m.kills == 3 and m.losses == 1 and m.solo_kills == 1
    assert round(m.danger_ratio, 2) == 0.75
    assert m.avg_gang_size == 1.0  # one attacker per fixture kill
    assert len(m.top_ships) >= 1 and len(m.top_systems) >= 1


@pytest.mark.django_db
def test_rebuild_member_metrics(combat):
    from apps.killboard.models import CombatMetric
    from apps.killboard.stats import rebuild_member_metrics

    assert rebuild_member_metrics() >= 1
    m = CombatMetric.objects.get(entity_type="character", entity_id=1001, window="all")
    assert m.kills == 3 and m.solo_kills == 1


@pytest.mark.django_db
def test_pilot_analytics_payload(combat):
    p = analytics.pilot_analytics(1001, use_cache=False)
    assert p["card"]["kills"] == 3
    assert p["monthly"]["kills"][-1] == 3
    assert any(r["ship_type_id"] == 22456 for r in p["ships"]["flown"])
    assert p["systems"][0]["system_id"] == 30000142


@pytest.mark.django_db
def test_pilot_page_access_and_render(client, django_user_model, combat):
    outsider = django_user_model.objects.create(username="eve:9001")
    client.force_login(outsider)
    assert client.get("/killboard/pilot/1001/").status_code == 403

    client.logout()
    member = django_user_model.objects.create(username="eve:9002")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    resp = client.get("/killboard/pilot/1001/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Combat analytics" in html and "Kills &amp; losses per month" in html


# --- Tier 3: security band, comparison, battle visuals, exports -------------
@pytest.mark.django_db
def test_security_breakdown(combat):
    sb = analytics.security_breakdown()
    assert sb["killed"] == [{"name": "Nullsec", "count": 3}]  # fixture mails are nullsec
    assert sb["lost"] == [{"name": "Nullsec", "count": 1}]


@pytest.mark.django_db
def test_compare_pilots(combat):
    c = analytics.compare_pilots([1001, 1001])  # de-dups
    assert len(c["series"]) == 1
    assert c["series"][0]["character_id"] == 1001
    assert c["series"][0]["kills"][-1] == 3
    assert c["table"][0]["kills"] == 3
    assert len(c["labels"]) == 12


@pytest.mark.django_db
def test_compare_view_add_remove_and_cap(client, django_user_model, combat):
    outsider = django_user_model.objects.create(username="eve:9101")
    client.force_login(outsider)
    assert client.get("/killboard/compare/").status_code == 403

    client.logout()
    member = django_user_model.objects.create(username="eve:9102")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    resp = client.get("/killboard/compare/?pilots=&add=1001")
    assert resp.status_code == 200 and 1001 in resp.context["selected_ids"]
    # remove
    resp = client.get("/killboard/compare/?pilots=1001&remove=1001")
    assert 1001 not in resp.context["selected_ids"]
    # cap at 5
    resp = client.get("/killboard/compare/?pilots=1,2,3,4,5,6,7")
    assert len(resp.context["selected_ids"]) == 5


@pytest.mark.django_db
def test_battle_report_renders_charts(client, django_user_model, combat):
    from apps.killboard.battle import generate_battle_report

    report = generate_battle_report(30000142, hours=48)
    assert report is not None
    member = django_user_model.objects.create(username="eve:9103")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    resp = client.get(f"/killboard/battles/{report.pk}/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "ISK lost by side" in html
    assert 'id="br-sides-data"' in html and 'id="br-ships-data"' in html


@pytest.mark.django_db
def test_dashboard_exposes_space_and_export_bar(client, django_user_model, combat):
    member = django_user_model.objects.create(username="eve:9104")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    html = client.get("/killboard/stats/").content.decode()
    assert "Where in space" in html and 'id="kb-space-data"' in html
    assert "Monthly CSV" in html and "Chart images (PNG)" in html
    assert "Compare pilots" in html


# --- Killfeed portal (the public /killboard/ home) --------------------------
@pytest.mark.django_db
def test_killfeed_overview_payload(combat):
    o = analytics.killfeed_overview(use_cache=False)
    assert o["summary"]["kills"] == 3 and o["summary"]["losses"] == 1
    assert len(o["spark"]) == 14 and o["spark"][-1] == 3 and o["spark_points"]
    assert o["biggest"][0]["value"] == Decimal("300000000")  # the 300M Punisher kill
    assert any(r["character_id"] == 1001 for r in o["top_killers"])  # the home attacker
    assert o["active_systems"][0]["system_id"] == 30000142
    assert o["active_systems"][0]["count"] == 4  # 3 kills + 1 loss (NPC excluded)


@pytest.mark.django_db
def test_killboard_home_renders_portal(client, combat):
    cache.clear()
    resp = client.get("/killboard/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Biggest kills" in html       # showcase strip
    assert "Top killers" in html and "Most active" in html  # side rail
    assert "Kills · last 14 days" in html  # hero sparkline
    assert "final blow" in html          # enriched rows


@pytest.mark.django_db
def test_killfeed_row_annotations(client, settings, db):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    km = _km(40, role=Killmail.HomeRole.ATTACKER)
    # Add two more attackers (3 total) so the involved-count badge shows.
    for i in (2, 3):
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=i, character_id=1000 + i,
            corporation_id=HOME, ship_type_id=587, final_blow=False, damage_done=50,
        )
    resp = client.get("/killboard/")
    row = resp.context["page"].object_list[0]
    assert row.attacker_count == 3
    assert row.final_blowers[0].character_id == 1001  # the prefetched final-blower


@pytest.mark.django_db
def test_old_kills_drop_out_of_heatmap_window(db, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _km(20, role=Killmail.HomeRole.ATTACKER, when=timezone.now() - timedelta(days=200))
    h = analytics.activity_heatmap(days=90)
    assert h["total"] == 0  # the 200-day-old kill is outside the 90-day window


# --- Access control ---------------------------------------------------------
@pytest.mark.django_db
def test_anonymous_is_sent_to_login(client, combat):
    resp = client.get("/killboard/stats/")
    assert resp.status_code == 302  # @login_required bounces anonymous visitors


@pytest.mark.django_db
def test_authenticated_outsider_is_forbidden(client, django_user_model, combat):
    user = django_user_model.objects.create(username="eve:8001")
    client.force_login(user)
    resp = client.get("/killboard/stats/")
    assert resp.status_code == 403
    assert "corp &amp; alliance only" in resp.content.decode()


@pytest.mark.django_db
def test_corp_member_sees_dashboard(client, django_user_model, combat):
    user = django_user_model.objects.create(username="eve:8002")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)
    resp = client.get("/killboard/stats/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Combat statistics" in html
    assert 'id="kb-monthly-data"' in html  # chart payload embedded
    assert 'id="kb-ships-data"' in html
    assert "chart.umd.js" in html.lower()  # self-hosted Chart.js loaded on this page
    # House rule: never name competitors in the UI.
    assert "pushx" not in html.lower() and "zkillboard" not in html.lower()


@pytest.mark.django_db
def test_registered_alliance_pilot_sees_dashboard(client, django_user_model, settings, combat):
    from apps.corporation.models import EveAlliance, EveCorporation
    from apps.sso.models import EveCharacter

    alliance = EveAlliance.objects.create(alliance_id=HOME_ALLIANCE, name="Home Alliance")
    EveCorporation.objects.create(corporation_id=HOME, name="Home", alliance=alliance)
    user = django_user_model.objects.create(username="eve:8003")
    EveCharacter.objects.create(character_id=8003, user=user, name="Ally", alliance_id=HOME_ALLIANCE)
    client.force_login(user)
    resp = client.get("/killboard/stats/")
    assert resp.status_code == 200
    assert "Combat statistics" in resp.content.decode()
