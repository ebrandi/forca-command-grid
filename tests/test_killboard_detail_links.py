"""KB-22 external reference links + KB-25 "Request SRP" on the killmail detail page.

Covers: zkill/EveWho outbound links for the killmail, victim and a grouped attacker party;
the native fit window staying free of any outbound reference link; and the owner-only
"Request SRP" affordance that reuses the apps/srp submit flow (button visibility + a POST
that files a claim and returns to the detail page + the honest ineligible explanation).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.killboard.ingest import ingest_killmail
from apps.market.models import MarketPrice

HOME = 98000001
RIFTER = 587
AUTOCANNON = 484


def _seed_prices(prices: dict[int, int]) -> None:
    for type_id, sell_min in prices.items():
        MarketPrice.objects.create(
            type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
            sell_min=Decimal(sell_min),
        )


def _body(kid: int = 100001, victim_corp: int = 99) -> dict:
    """A kill: victim char 2001 in a Rifter (587) with one destroyed autocannon (484);
    three attackers across corp 99 (×2) and the home corp (×1)."""
    return {
        "killmail_id": kid,
        "killmail_time": "2026-06-20T12:00:00Z",
        "solar_system_id": 30002053,
        "victim": {
            "character_id": 2001, "corporation_id": victim_corp,
            "ship_type_id": RIFTER, "damage_taken": 1000,
            "items": [{"item_type_id": AUTOCANNON, "flag": 27, "quantity_destroyed": 1}],
        },
        "attackers": [
            {"character_id": 3001, "corporation_id": 99, "ship_type_id": RIFTER,
             "final_blow": True, "damage_done": 600},
            {"character_id": 3002, "corporation_id": 99, "ship_type_id": 600,
             "damage_done": 300},
            {"character_id": 3003, "corporation_id": HOME, "ship_type_id": RIFTER,
             "damage_done": 100},
        ],
    }


def _member(django_user_model, username, cid, role):
    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role

    u = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=u, name=username,
                                is_main=True, is_corp_member=True)
    return u


def _doctrine_with_rifter():
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Doctrine", category=cat, priority=90,
                                status=Doctrine.Status.ACTIVE)
    DoctrineFit.objects.create(
        doctrine=d, name="Rifter", ship_type_id=RIFTER,
        modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
    )
    return d


def _default_rule():
    from apps.srp.models import SrpRule
    return SrpRule.objects.create(doctrine=None, basis=SrpRule.Basis.FIT, max_payout=0, active=True)


# --- KB-22: external reference links ------------------------------------------
@pytest.mark.django_db
def test_external_links_render_for_killmail_victim_and_party(client, sde):
    _seed_prices({RIFTER: 5_000_000})
    # Victim corp distinct from the attacker corps so the victim-corp link is unambiguous.
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=98000055))
    html = client.get("/killboard/100001/").content

    # The killmail itself -> zKillboard.
    assert b"https://zkillboard.com/kill/100001/" in html

    # Victim entities -> zkill + EveWho.
    assert b"https://zkillboard.com/character/2001/" in html
    assert b"https://evewho.com/character/2001" in html
    assert b"https://zkillboard.com/corporation/98000055/" in html
    assert b"https://evewho.com/corporation/98000055" in html

    # A grouped attacker party (corp 99) -> zkill + EveWho.
    assert b"https://zkillboard.com/corporation/99/" in html
    assert b"https://evewho.com/corporation/99" in html

    # An attacker pilot row -> zkill + EveWho.
    assert b"https://zkillboard.com/character/3001/" in html
    assert b"https://evewho.com/character/3001" in html

    # Every outbound link opens safely in a new tab.
    assert b'rel="noopener noreferrer"' in html


@pytest.mark.django_db
def test_fit_window_has_no_outbound_links(client, sde):
    _seed_prices({RIFTER: 5_000_000, AUTOCANNON: 100_000})
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=98000055))
    html = client.get("/killboard/100001/").content

    # The native fit window rendered...
    assert b"Fitting &amp; cargo" in html
    # ...and nothing from the fit window onward (fit + deviation + related + comments) carries an
    # outbound reference link — the KB-22 links live only on the entity blocks above it.
    after_fit = html.split(b"Fitting (KB-21b)", 1)[1]
    assert b"zkillboard.com" not in after_fit
    assert b"evewho.com" not in after_fit


# --- KB-25: Request SRP on the detail page ------------------------------------
@pytest.mark.django_db
def test_request_srp_button_visibility(client, django_user_model, sde):
    from apps.srp.models import SrpClaim

    _seed_prices({RIFTER: 500_000, AUTOCANNON: 100_000})
    _doctrine_with_rifter()
    _default_rule()
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=HOME))
    owner = _member(django_user_model, "owner", 2001, "member")
    url = "/killboard/100001/"

    # Owner of an eligible, unclaimed loss sees the button + the doctrine verdict.
    client.force_login(owner)
    html = client.get(url).content
    assert b"Request SRP" in html
    assert b"Rifter Doctrine" in html

    # A non-owner member never sees the request affordance.
    client.force_login(_member(django_user_model, "peer", 3001, "member"))
    assert b"Request SRP" not in client.get(url).content

    # Once a claim exists, the button gives way to the status chip (even for the owner).
    SrpClaim.objects.create(killmail_id=100001, claimant=owner,
                            status=SrpClaim.Status.SUBMITTED)
    client.force_login(owner)
    html = client.get(url).content
    assert b"Request SRP" not in html
    assert b"SRP pending" in html


@pytest.mark.django_db
def test_request_srp_post_files_claim_and_returns_to_detail(client, django_user_model, sde):
    from apps.srp.models import SrpClaim

    _seed_prices({RIFTER: 500_000, AUTOCANNON: 100_000})
    _doctrine_with_rifter()
    _default_rule()
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=HOME))
    owner = _member(django_user_model, "owner", 2001, "member")
    client.force_login(owner)

    detail = "/killboard/100001/"
    resp = client.post(reverse("srp:claim"), {"killmail_id": 100001, "next": detail})

    # A single claim is filed for this loss, by the owner...
    claim = SrpClaim.objects.get(killmail_id=100001)
    assert claim.claimant_id == owner.id
    # ...and the button returns the pilot to the killmail page (not /srp/mine).
    assert resp.status_code == 302
    assert resp.url == detail

    # The detail page now shows the status chip in place of the button.
    html = client.get(detail).content
    assert b"SRP pending" in html
    assert b"Request SRP" not in html


@pytest.mark.django_db
def test_ineligible_owner_sees_explanation_not_button(client, django_user_model, sde):
    # No doctrine fit for the hull -> require_doctrine makes the loss ineligible.
    _seed_prices({RIFTER: 500_000})
    _default_rule()
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=HOME))
    owner = _member(django_user_model, "owner", 2001, "member")

    client.force_login(owner)
    html = client.get("/killboard/100001/").content
    assert b"Request SRP" not in html
    assert b"Not eligible for SRP" in html
    assert b"active doctrine hull" in html  # the honest reason (apostrophe is HTML-escaped)


@pytest.mark.django_db
def test_srp_claim_ignores_offsite_next(client, django_user_model, sde):
    """The tolerant ``next`` param must not become an open redirect."""
    from apps.srp.models import SrpClaim

    _seed_prices({RIFTER: 500_000, AUTOCANNON: 100_000})
    _doctrine_with_rifter()
    _default_rule()
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=HOME))
    owner = _member(django_user_model, "owner", 2001, "member")
    client.force_login(owner)

    resp = client.post(reverse("srp:claim"),
                       {"killmail_id": 100001, "next": "https://evil.example/phish"})
    assert SrpClaim.objects.filter(killmail_id=100001).exists()
    # Falls back to the SRP page rather than honouring an off-site target.
    assert resp.status_code == 302
    assert resp.url == reverse("srp:mine")
