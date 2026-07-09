"""Intel watchlists and battle reports: interactive CRUD + generation."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard.battle import generate_battle_report
from apps.killboard.intel import entry_activity
from apps.killboard.models import Killmail, KillmailParticipant, Watchlist, WatchlistEntry
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, role):
    user = django_user_model.objects.create(username=f"u{role}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _km(kid, system, ship, value, victim_corp, time, role_corp=None):
    km = Killmail.objects.create(
        killmail_id=kid, killmail_hash="h", killmail_time=time, solar_system_id=system,
        victim_ship_type_id=ship, victim_corporation_id=victim_corp,
        total_value=Decimal(value), involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
    )
    KillmailParticipant.objects.create(
        killmail=km, role=KillmailParticipant.Role.VICTIM, seq=0,
        corporation_id=victim_corp, ship_type_id=ship,
    )
    if role_corp:
        KillmailParticipant.objects.create(
            killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=1,
            corporation_id=role_corp, ship_type_id=ship, final_blow=True,
        )
    return km


@pytest.mark.django_db
def test_entry_activity_counts_kills_and_losses(sde):
    now = timezone.now()
    # Corp 500 loses km1 (victim) and scores km2 (attacker).
    _km(1, 30000142, 587, "10000000", 500, now, role_corp=999)
    _km(2, 30000142, 587, "20000000", 999, now, role_corp=500)
    wl = Watchlist.objects.create(name="Targets")
    entry = WatchlistEntry.objects.create(
        watchlist=wl, entity_type=WatchlistEntry.EntityType.CORPORATION, entity_id=500
    )
    act = entry_activity(entry)
    assert act["losses"] == 1 and act["kills"] == 1 and act["total"] == 2


@pytest.mark.django_db
def test_officer_creates_watchlist_and_entries_member_cannot(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    member = _user(django_user_model, rbac.ROLE_MEMBER)

    client.force_login(member)
    # Member can view, cannot create.
    assert client.get("/killboard/intel/").status_code == 200
    assert client.post("/killboard/intel/create/", {"name": "X", "purpose": ""}).status_code == 403

    client.force_login(officer)
    client.post("/killboard/intel/create/", {"name": "Gate campers", "purpose": "watch"})
    wl = Watchlist.objects.get(name="Gate campers")
    client.post(f"/killboard/intel/{wl.pk}/add/", {
        "entity_type": "corporation", "entity_id": 500, "note": "hostile",
    })
    assert wl.entries.count() == 1
    entry = wl.entries.first()
    client.post(f"/killboard/intel/{wl.pk}/entries/{entry.id}/remove/")
    assert wl.entries.count() == 0


@pytest.mark.django_db
def test_generate_battle_report(client, django_user_model, sde):
    now = timezone.now()
    _km(10, 30002053, 587, "100000000", 500, now, role_corp=999)
    _km(11, 30002053, 588, "50000000", 999, now, role_corp=500)
    report = generate_battle_report(30002053, hours=24, title="Otitoh brawl")
    assert report is not None
    assert report.killmails.count() == 2
    corps = {s["corporation_id"]: s for s in report.sides["corporations"]}
    assert corps[500]["losses"] == 1 and corps[500]["kills"] == 1
    assert report.ship_breakdown  # ships destroyed tallied

    # Empty window -> no report.
    assert generate_battle_report(30000142, hours=24) is None

    # Officer route works; member is blocked from generating.
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    client.force_login(officer)
    resp = client.post("/killboard/battles/create/", {"system_id": 30002053, "hours": 24, "title": "t"})
    assert resp.status_code == 302
    assert client.get(f"/killboard/battles/{report.pk}/").status_code == 200

    member = _user(django_user_model, rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.post("/killboard/battles/create/", {"system_id": 30002053, "hours": 24}).status_code == 403


@pytest.mark.django_db
def test_battle_report_default_title_resolves_system_name(sde):
    now = timezone.now()
    _km(20, 30002053, 587, "100000000", 500, now, role_corp=999)
    report = generate_battle_report(30002053, hours=24)  # no custom title
    # The default title uses the system NAME, never the raw id.
    assert report.title.startswith("Battle in ")
    assert "30002053" not in report.title and "system 3" not in report.title


@pytest.mark.django_db
def test_system_search(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER))
    rows = client.get("/killboard/intel/systems/?q=Jita").json()
    assert any(r["type_id"] == 30000142 for r in rows)
