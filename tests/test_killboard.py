"""Killmail ingestion + valuation/points tests."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import Killmail, SecBand
from apps.killboard.valuation import compute_points
from apps.market.models import MarketPrice
from core.mixins import Source


def _seed_prices(prices: dict[int, int]) -> None:
    """Seed Jita-sell MarketPrice rows so valuation has a real market signal
    (the engine no longer trusts SDE base_price — see apps/market/pricing.py)."""
    for type_id, sell_min in prices.items():
        MarketPrice.objects.create(
            type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
            sell_min=Decimal(sell_min),
        )

BODY = {
    "killmail_id": 100001,
    "killmail_time": "2026-06-20T12:00:00Z",
    "solar_system_id": 30002053,  # Otitoh, sec 0.3 -> lowsec
    "victim": {
        "character_id": 2001,
        "corporation_id": 98000001,  # FORCA home corp (test settings)
        "ship_type_id": 587,
        "damage_taken": 1000,
        "items": [
            {"item_type_id": 484, "flag": 27, "quantity_destroyed": 1},
            {"item_type_id": 192, "flag": 5, "quantity_dropped": 100},
        ],
    },
    "attackers": [
        {
            "character_id": 3001,
            "corporation_id": 99,
            "ship_type_id": 587,
            "final_blow": True,
            "damage_done": 1000,
        }
    ],
}


@pytest.mark.django_db
def test_ingest_creates_killmail_with_values(sde):
    # hull 587 -> 380000, destroyed module 484 -> 12000, dropped ammo 192 -> 5
    _seed_prices({587: 380000, 484: 12000, 192: 5})
    km = ingest_killmail(100001, "hash1", source=Source.ESI_CORP, body=BODY)
    assert km.solar_system_id == 30002053
    assert km.region_id == 10000002
    assert km.sec_band == SecBand.LOWSEC
    assert km.involves_home_corp is True
    assert km.home_corp_role == Killmail.HomeRole.VICTIM
    assert km.is_solo is True
    assert km.participants.count() == 2  # 1 victim + 1 attacker
    assert km.items.count() == 2
    # hull(380000) + destroyed module(12000) = 392000; dropped ammo 100*5 = 500
    assert km.destroyed_value == 392000
    assert km.dropped_value == 500
    assert km.total_value == 392500
    assert km.points >= 1


@pytest.mark.django_db
def test_ingest_is_idempotent(sde):
    ingest_killmail(100001, "hash1", body=BODY)
    ingest_killmail(100001, "hash1", body=BODY)
    assert Killmail.objects.filter(killmail_id=100001).count() == 1


@pytest.mark.django_db
def test_points_blob_penalty(sde):
    km = ingest_killmail(100001, "hash1", body=BODY)
    solo_points = compute_points(km)
    # Add more attackers -> quadratic blob penalty reduces points.
    from apps.killboard.models import KillmailParticipant

    for i in range(1, 8):
        KillmailParticipant.objects.create(
            killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=i, character_id=4000 + i
        )
    blob_points = compute_points(km)
    assert blob_points < solo_points


@pytest.mark.django_db
def test_non_home_corp_kill_not_involved(sde):
    body = dict(BODY)
    body = {**BODY, "killmail_id": 100002, "victim": {**BODY["victim"], "corporation_id": 12345}}
    km = ingest_killmail(100002, "h2", body=body)
    assert km.involves_home_corp is False
    assert km.home_corp_role == Killmail.HomeRole.NONE


@pytest.mark.django_db
def test_killboard_alliance_and_side_filters(client, sde):
    """KB-03: the killfeed gains an alliance filter, and a victim/attacker side
    toggle that flips the pilot/corp/alliance filters between sides."""
    _seed_prices({587: 380000})
    # Home corp (98000001) is the VICTIM; its alliance is 11111, killed by enemy
    # alliance 22222.
    loss = {
        "killmail_id": 100001, "killmail_time": "2026-06-20T12:00:00Z", "solar_system_id": 30002053,
        "victim": {"character_id": 2001, "corporation_id": 98000001, "alliance_id": 11111,
                   "ship_type_id": 587, "damage_taken": 1000, "items": []},
        "attackers": [{"character_id": 3001, "corporation_id": 99, "alliance_id": 22222,
                       "ship_type_id": 587, "final_blow": True, "damage_done": 1000}],
    }
    # Home corp is an ATTACKER (a kill); the victim is in enemy alliance 22222.
    kill = {
        "killmail_id": 100002, "killmail_time": "2026-06-21T12:00:00Z", "solar_system_id": 30002053,
        "victim": {"character_id": 4001, "corporation_id": 99, "alliance_id": 22222,
                   "ship_type_id": 587, "damage_taken": 1000, "items": []},
        "attackers": [{"character_id": 2001, "corporation_id": 98000001, "alliance_id": 11111,
                       "ship_type_id": 587, "final_blow": True, "damage_done": 1000}],
    }
    ingest_killmail(100001, "h1", body=loss)
    ingest_killmail(100002, "h2", body=kill)

    # Victim-side: alliance 22222 is the victim only on the kill (100002).
    r = client.get("/killboard/", {"alliance_id": 22222})
    assert b"/killboard/100002" in r.content
    assert b"/killboard/100001" not in r.content

    # Attacker-side: alliance 22222 only attacks on the loss (100001).
    r = client.get("/killboard/", {"alliance_id": 22222, "side": "attacker"})
    assert b"/killboard/100001" in r.content
    assert b"/killboard/100002" not in r.content


@pytest.mark.django_db
def test_killboard_htmx_returns_feed_fragment(client, sde):
    """D5: an htmx request returns only the #kb-feed fragment — no full-page shell,
    no hero/side-rail — so filtering/paging swaps just the feed."""
    full = client.get("/killboard/")
    assert full.status_code == 200
    assert b'id="kb-feed"' in full.content
    assert b"Top killers" in full.content  # side rail is on the full page

    frag = client.get("/killboard/", HTTP_HX_REQUEST="true")
    assert frag.status_code == 200
    assert b'id="kb-feed"' in frag.content
    assert b"Top killers" not in frag.content      # side rail excluded from fragment
    assert b"<!doctype html" not in frag.content.lower()  # no document shell


@pytest.mark.django_db
def test_killboard_crawler_hardening(client, sde):
    """Faceted-crawler defense: the canonical landing page stays indexable but its
    drill-down links are rel=nofollow, while any filtered/query-string variant is
    marked noindex — so AI/SEO crawlers don't enumerate the infinite filter space."""
    canonical = client.get("/killboard/")
    assert canonical.status_code == 200
    # Canonical page: indexable (no robots meta) ...
    assert b'name="robots"' not in canonical.content
    # ... but the filter/drill-down links must not be followed into the trap.
    assert b'rel="nofollow"' in canonical.content

    # Any filtered / drill-down page (carries a query string) is non-canonical.
    filtered = client.get("/killboard/", {"character_id": 2001})
    assert filtered.status_code == 200
    assert b'content="noindex, nofollow"' in filtered.content


def _rifter_doctrine():
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Rifter Doctrine", category=cat, priority=90)
    return DoctrineFit.objects.create(doctrine=doctrine, name="Rifter", ship_type_id=587, modules=[])


@pytest.mark.django_db
def test_killmail_doctrine_tagging(sde):
    """KB-13: a home-corp loss whose hull matches an active doctrine fit is tagged
    at ingest; kills and non-doctrine hulls are not."""
    fit = _rifter_doctrine()

    # Home-corp loss on the doctrine hull (587) -> tagged.
    loss = ingest_killmail(100001, "h1", body=BODY)
    assert loss.home_corp_role == Killmail.HomeRole.VICTIM
    assert loss.doctrine_fit_id == fit.id

    # Loss on a non-doctrine hull -> untagged.
    other = {**BODY, "killmail_id": 100002, "victim": {**BODY["victim"], "ship_type_id": 588}}
    assert ingest_killmail(100002, "h2", body=other).doctrine_fit_id is None

    # A kill (home corp is the attacker; the doctrine hull is the enemy victim) ->
    # not tagged: doctrine_fit means "our lost ship matched our doctrine".
    kill = {
        "killmail_id": 100003, "killmail_time": "2026-06-20T12:00:00Z", "solar_system_id": 30002053,
        "victim": {"character_id": 7, "corporation_id": 99, "ship_type_id": 587, "damage_taken": 1, "items": []},
        "attackers": [{"character_id": 8, "corporation_id": 98000001, "final_blow": True, "damage_done": 1}],
    }
    km3 = ingest_killmail(100003, "h3", body=kill)
    assert km3.home_corp_role == Killmail.HomeRole.ATTACKER
    assert km3.doctrine_fit_id is None


@pytest.mark.django_db
def test_retag_doctrine_fits_backfill(sde):
    """KB-13: the backfill command tags losses ingested before the doctrine existed."""
    from django.core.management import call_command

    loss = ingest_killmail(100001, "h1", body=BODY)
    assert loss.doctrine_fit_id is None  # no doctrine yet

    fit = _rifter_doctrine()
    call_command("retag_doctrine_fits")
    loss.refresh_from_db()
    assert loss.doctrine_fit_id == fit.id


def _doctrine_fit_with_modules(modules):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Rifter Doctrine", category=cat, priority=90)
    return DoctrineFit.objects.create(
        doctrine=doctrine, name="Rifter", ship_type_id=587, modules=modules
    )


@pytest.mark.django_db
def test_fit_deviation_computed_at_ingest(sde):
    """KB-14: the loss's fitted modules (cargo excluded) are diffed against the
    canonical fit. BODY fits gun 484 (HiSlot flag 27) + ammo 192 in cargo (flag 5)."""
    from apps.killboard.models import FitDeviation

    # The fit requires gun 485, which the loss didn't have; the loss fitted 484.
    fit = _doctrine_fit_with_modules([{"type_id": 485, "quantity": 1, "name": "Gun II"}])
    loss = ingest_killmail(100001, "h1", body=BODY)

    dev = FitDeviation.objects.get(killmail=loss)
    assert dev.doctrine_fit_id == fit.id
    assert dev.missing == [{"type_id": 485, "quantity": 1}]  # required, absent
    assert dev.extra == [{"type_id": 484, "quantity": 1}]    # fitted, off-doctrine
    assert dev.is_clean is False


@pytest.mark.django_db
def test_fit_deviation_clean_when_matching(sde):
    """A loss whose fitted modules match the doctrine fit (cargo ignored) is clean."""
    from apps.killboard.models import FitDeviation

    _doctrine_fit_with_modules([{"type_id": 484, "quantity": 1, "name": "Gun"}])
    loss = ingest_killmail(100001, "h1", body=BODY)
    dev = FitDeviation.objects.get(killmail=loss)
    assert dev.missing == [] and dev.extra == []
    assert dev.is_clean is True


@pytest.mark.django_db
def test_fit_deviation_cargo_spare_not_missing(sde):
    """KB-14: a doctrine consumable carried as a cargo spare is NOT missing —
    `missing` counts everything aboard, while `extra` is fitted-slot only."""
    from apps.killboard.models import FitDeviation

    # Doctrine wants fitted gun 485 + 50× ammo 192. BODY fitted gun 484 (flag 27)
    # and carried 100× ammo 192 in cargo (flag 5).
    _doctrine_fit_with_modules([
        {"type_id": 485, "quantity": 1, "name": "Gun II"},
        {"type_id": 192, "quantity": 50, "name": "Ammo"},
    ])
    loss = ingest_killmail(100001, "h1", body=BODY)
    dev = FitDeviation.objects.get(killmail=loss)
    # Only the genuinely-absent fitted module 485 is missing; cargo ammo 192 isn't.
    assert {m["type_id"] for m in dev.missing} == {485}
    # `extra` is fitted-only, so cargo 192 isn't flagged — just off-doctrine gun 484.
    assert dev.extra == [{"type_id": 484, "quantity": 1}]


@pytest.mark.django_db
def test_fit_deviation_privacy_on_detail(client, django_user_model, sde):
    """KB-14 privacy: a pilot's fit deviation is shown only to that pilot and to
    officers — never to peers or anonymous visitors (PRD §B5)."""
    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role
    from core import rbac

    _doctrine_fit_with_modules([{"type_id": 485, "quantity": 1, "name": "Gun II"}])
    ingest_killmail(100001, "h1", body=BODY)  # victim character 2001
    url = "/killboard/100001/"

    def _member(username, cid, role):
        user = django_user_model.objects.create(username=username)
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
        EveCharacter.objects.create(character_id=cid, user=user, name=username,
                                    is_main=True, is_corp_member=True)
        return user

    # Assert on text inside the gated section (the HTML comment is always present).
    marker = b"Missing from your fit"

    # Anonymous visitor: hidden.
    assert marker not in client.get(url).content

    # The pilot who lost the ship (owns victim char 2001): visible.
    client.force_login(_member("owner", 2001, rbac.ROLE_MEMBER))
    assert marker in client.get(url).content

    # A different member: hidden.
    client.force_login(_member("peer", 3001, rbac.ROLE_MEMBER))
    assert marker not in client.get(url).content

    # An officer: visible.
    client.force_login(_member("officer", 4001, rbac.ROLE_OFFICER))
    assert marker in client.get(url).content
