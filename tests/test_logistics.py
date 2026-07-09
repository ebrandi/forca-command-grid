"""Freight service: pricing, route facts, audience access, and the flow."""
from __future__ import annotations

import json

import pytest
import responses
from django.core.cache import cache

from apps.identity.models import RoleAssignment
from apps.logistics.models import Audience, CourierContract, RateCard
from apps.logistics.pricing import quote
from apps.logistics.services import active_rate_card, can_access, invalidate_audience_cache
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001


def _card(**overrides) -> RateCard:
    card = RateCard()
    for k, v in overrides.items():
        setattr(card, k, v)
    return card


# --- Pricing (math is locked to known reference points) ---------------------
def test_freighter_quote_applies_multiplier():
    # 2,250,000 × 11 warps ×1.0 collateral = 24,750,000 full; ×0.80 = 19,800,000.
    q = quote(_card(), ship_class="freighter", jumps=10, volume_m3=300_000,
              collateral=1_000_000_000, sec_band="highsec")
    assert q.ok
    assert int(q.reward) == 19_800_000
    assert q.breakdown["base_price"] == 24_750_000
    # Visible line items reconcile to the final reward (no separate "discount").
    assert sum(line["isk"] for line in q.breakdown["lines"]) == int(q.reward)


def test_freighter_collateral_multiplier_tier():
    # 4b collateral → ×4 tier: 24,750,000 × 4 = 99,000,000; ×0.80 = 79,200,000.
    q = quote(_card(), ship_class="freighter", jumps=10, volume_m3=300_000,
              collateral=4_000_000_000, sec_band="highsec")
    assert int(q.reward) == 79_200_000


def test_jump_freighter_additive_model():
    # 200M base + 5×100M + 50M collateral fee = 750M; ×0.80 = 600M.
    q = quote(_card(), ship_class="jf", jumps=6, lowsec_jumps=5, volume_m3=300_000,
              collateral=8_000_000_000, sec_band="lowsec")
    assert int(q.reward) == 600_000_000


def test_volume_cap_rejected():
    q = quote(_card(), ship_class="dst", jumps=5, volume_m3=99_999,
              collateral=0, sec_band="highsec")
    assert not q.ok and "exceeds" in q.error


def test_collateral_cap_rejected():
    q = quote(_card(), ship_class="freighter", jumps=5, volume_m3=1000,
              collateral=9_000_000_000, sec_band="highsec")
    assert not q.ok and "collateral" in q.error.lower()


def test_minimum_reward_floor():
    q = quote(_card(), ship_class="freighter", jumps=1, volume_m3=1000,
              collateral=0, sec_band="highsec")
    assert int(q.reward) == 4_500_000
    assert q.breakdown["min_reward_applied"] is True


def test_custom_multiplier_changes_price():
    q = quote(_card(discount=1), ship_class="freighter", jumps=10, volume_m3=1000,
              collateral=1_000_000_000, sec_band="highsec")
    assert int(q.reward) == 24_750_000  # full rate when multiplier is 1.0


# --- Route facts ------------------------------------------------------------
@responses.activate
@pytest.mark.django_db
def test_route_facts_counts_jumps_and_worst_band(sde):
    from apps.sde.models import SdeRegion, SdeSolarSystem

    region, _ = SdeRegion.objects.get_or_create(region_id=10000002, defaults={"name": "The Forge"})
    for sid, sec, nm in [(30000142, 0.9, "Jita"), (30000144, 0.3, "Mid"), (30000145, 0.8, "Dest")]:
        SdeSolarSystem.objects.update_or_create(
            system_id=sid, defaults={"region": region, "name": nm, "security": sec}
        )
    responses.add(
        responses.POST, "https://esi.evetech.net/route/30000142/30000145/",
        body=json.dumps({"route": [30000142, 30000144, 30000145]}), status=200,
        content_type="application/json",
    )
    from apps.logistics.routing import route_facts
    cache.clear()
    facts = route_facts(30000142, 30000145)
    assert facts["jumps"] == 2
    assert facts["lowsec_jumps"] == 1       # the 0.3 system
    assert facts["sec_band"] == "lowsec"
    # The new body-based POST /route is used with the "Safer" preference.
    req = responses.calls[-1].request
    assert req.method == "POST"
    assert json.loads(req.body)["preference"] == "Safer"


# --- Audience access control ------------------------------------------------
def _set_audience(value):
    card = active_rate_card()
    card.audience = value
    card.save(update_fields=["audience"])
    invalidate_audience_cache()


@pytest.mark.django_db
def test_public_audience_allows_anonymous(client):
    _set_audience(Audience.PUBLIC)
    from django.contrib.auth.models import AnonymousUser
    assert can_access(AnonymousUser()) is True


@pytest.mark.django_db
def test_disabled_audience_blocks_everyone(client, django_user_model):
    _set_audience(Audience.DISABLED)
    from django.contrib.auth.models import AnonymousUser
    user = django_user_model.objects.create(username="eve:7001")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    assert can_access(AnonymousUser()) is False
    assert can_access(user) is False


@pytest.mark.django_db
def test_corp_audience_requires_member(django_user_model):
    _set_audience(Audience.CORP)
    from django.contrib.auth.models import AnonymousUser
    member = django_user_model.objects.create(username="eve:7002")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    outsider = django_user_model.objects.create(username="eve:7003")
    assert can_access(AnonymousUser()) is False
    assert can_access(member) is True
    assert can_access(outsider) is False


@pytest.mark.django_db
def test_alliance_audience_allows_registered_alliance_pilot(settings, django_user_model):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    from apps.corporation.models import EveAlliance, EveCorporation
    from apps.sso.models import EveCharacter

    alliance = EveAlliance.objects.create(alliance_id=HOME_ALLIANCE, name="Home Alliance")
    EveCorporation.objects.create(corporation_id=HOME_CORP, name="Home", alliance=alliance)
    _set_audience(Audience.ALLIANCE)

    ally = django_user_model.objects.create(username="eve:7004")
    EveCharacter.objects.create(character_id=7004, user=ally, name="Ally", alliance_id=HOME_ALLIANCE)
    stranger = django_user_model.objects.create(username="eve:7005")
    EveCharacter.objects.create(character_id=7005, user=stranger, name="Stranger", alliance_id=42)

    assert can_access(ally) is True
    assert can_access(stranger) is False


# --- View flow + no competitor naming ---------------------------------------
@pytest.mark.django_db
def test_calculator_public_render_has_no_competitor_name(client, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _set_audience(Audience.PUBLIC)
    html = client.get("/freight/").content.decode().lower()
    assert html.count("freight") > 0
    assert "pushx" not in html and "push x" not in html
    assert "red-frog" not in html and "red frog" not in html


@pytest.mark.django_db
def test_calculator_blocks_anonymous_when_members_only(client):
    _set_audience(Audience.ALLIANCE)
    resp = client.get("/freight/")
    assert resp.status_code == 403  # unavailable page, not the calculator


@pytest.mark.django_db
def test_home_corp_name_resolves_and_workflow_is_explained(client, django_user_model, settings, sde):
    from apps.corporation.access import home_corp_name
    from apps.corporation.models import EveCorporation
    from apps.sso.models import EveCharacter

    settings.FORCA_HOME_CORP_ID = HOME_CORP
    settings.FORCA_CORP_NAME = "Branding Fallback"
    # Falls back to the configured name when the corp row/name is unknown…
    assert home_corp_name() == "Branding Fallback"
    EveCorporation.objects.create(corporation_id=HOME_CORP, name="Forças Armadas")
    assert home_corp_name() == "Forças Armadas"  # …then prefers the resolved name.

    _set_audience(Audience.PUBLIC)
    user = django_user_model.objects.create(username="eve:7300")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=7300, user=user, name="Cap", is_main=True, is_corp_member=True)
    client.force_login(user)
    html = client.get("/freight/").content.decode()
    # The page names the app-owning corp and explains the to-home-corp contract flow.
    assert "Forças Armadas" in html
    assert "courier contract to" in html


@responses.activate
@pytest.mark.django_db
def test_member_can_post_claim_and_deliver(client, django_user_model, settings, sde):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _set_audience(Audience.CORP)
    from apps.sde.models import SdeRegion, SdeSolarSystem
    from apps.sso.models import EveCharacter

    # 11 high-sec systems → a 10-jump route, mocked via ESI so the freighter
    # reward is the known 19,800,000 reference point.
    region, _ = SdeRegion.objects.get_or_create(region_id=10000002, defaults={"name": "The Forge"})
    path = list(range(30001000, 30001011))
    for sid in path:
        SdeSolarSystem.objects.update_or_create(
            system_id=sid, defaults={"region": region, "name": f"Sys{sid}", "security": 0.9}
        )
    responses.add(
        responses.POST, f"https://esi.evetech.net/route/{path[0]}/{path[-1]}/",
        body=json.dumps({"route": path}), status=200, content_type="application/json",
    )
    cache.clear()

    user = django_user_model.objects.create(username="eve:7100")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=7100, user=user, name="Hauler", is_main=True, is_corp_member=True)
    client.force_login(user)

    resp = client.post("/freight/contracts/post/", {
        "ship_class": "freighter", "volume_m3": 300000, "collateral": 1000000000,
        "origin_kind": "system", "origin_id": path[0], "origin_system_id": path[0], "origin_name": "ignored",
        "dest_kind": "system", "dest_id": path[-1], "dest_system_id": path[-1], "dest_name": "ignored",
        "post_as": "character",
    })
    assert resp.status_code == 302
    contract = CourierContract.objects.get()
    assert contract.status == CourierContract.Status.OUTSTANDING
    assert int(contract.reward) == 19_800_000
    # Posted under the pilot's name (server-derived, not free text), with the
    # canonical system name re-derived from the SDE (the submitted name ignored).
    assert contract.posted_as_kind == "character" and contract.posted_as_name == "Hauler"
    assert contract.origin_name == f"Sys{path[0]}" and contract.origin_location_kind == "system"

    client.post(f"/freight/contracts/{contract.pk}/claim/")
    contract.refresh_from_db()
    assert contract.status == CourierContract.Status.IN_PROGRESS
    assert contract.assigned_user_id == user.id

    client.post(f"/freight/contracts/{contract.pk}/transition/", {"action": "delivered"})
    contract.refresh_from_db()
    assert contract.status == CourierContract.Status.DELIVERED

    # Delivering credits the hauler's contribution ledger (haul, in m³), keyed to
    # the contract so it can't double-count.
    from apps.pilots.models import ContributionEvent

    haul = ContributionEvent.objects.get(
        kind=ContributionEvent.Kind.HAUL, ref_type="courier_contract", ref_id=str(contract.pk)
    )
    assert haul.user_id == user.id
    assert haul.magnitude == 300000
    # Default weights require verification, so the self-report is provisional (0 pts).
    assert haul.points == 0


class _FakeESI:
    """Stand-in ESI client returning a fixed contracts list for get_paged."""

    def __init__(self, rows):
        self._rows = rows

    def get_paged(self, path, *, token=None, params=None):
        return self._rows


def _pending_haul(django_user_model, hauler_id=7100, volume=300000):
    user = django_user_model.objects.create(username=f"eve:{hauler_id}")
    contract = CourierContract.objects.create(
        origin_name="A", dest_name="B", volume_m3=volume, reward=1,
        status=CourierContract.Status.IN_PROGRESS,
        assigned_user=user, assigned_hauler_character_id=hauler_id,
    )
    return user, contract


@pytest.mark.django_db
def test_reconcile_verifies_a_matching_finished_contract(monkeypatch, django_user_model, settings):
    from apps.logistics import contracts_esi
    from apps.pilots.models import ContributionEvent

    settings.FORCA_HOME_CORP_ID = HOME_CORP
    monkeypatch.setattr(contracts_esi, "_director_contract_token", lambda corp_id: "tok")
    user, contract = _pending_haul(django_user_model)

    rows = [{"type": "courier", "acceptor_id": 7100, "volume": 300000,
             "status": "finished", "contract_id": 999}]
    result = contracts_esi.reconcile_courier_contracts(client=_FakeESI(rows))

    assert result["verified"] == 1
    contract.refresh_from_db()
    assert contract.verification_state == "verified"
    assert contract.esi_contract_id == 999
    # The provisional haul is upgraded to full points (default haul_points = 3).
    ev = ContributionEvent.objects.get(kind="haul", ref_id=str(contract.pk))
    assert ev.points == 3


@pytest.mark.django_db
def test_reconcile_marks_failed_and_revokes_credit(monkeypatch, django_user_model, settings):
    from apps.logistics import contracts_esi
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution

    settings.FORCA_HOME_CORP_ID = HOME_CORP
    monkeypatch.setattr(contracts_esi, "_director_contract_token", lambda corp_id: "tok")
    user, contract = _pending_haul(django_user_model)
    # A provisional credit exists from the self-report.
    record_contribution(user, ContributionEvent.Kind.HAUL, magnitude=300000, unit="m³",
                        ref_type="courier_contract", ref_id=str(contract.pk), points=0)

    rows = [{"type": "courier", "acceptor_id": 7100, "volume": 300000, "status": "failed",
             "contract_id": 1000}]
    result = contracts_esi.reconcile_courier_contracts(client=_FakeESI(rows))

    assert result["failed"] == 1
    contract.refresh_from_db()
    assert contract.verification_state == "failed"
    assert not ContributionEvent.objects.filter(kind="haul", ref_id=str(contract.pk)).exists()


@pytest.mark.django_db
def test_reconcile_leaves_unmatched_contract_unverified(monkeypatch, django_user_model, settings):
    from apps.logistics import contracts_esi

    settings.FORCA_HOME_CORP_ID = HOME_CORP
    monkeypatch.setattr(contracts_esi, "_director_contract_token", lambda corp_id: "tok")
    user, contract = _pending_haul(django_user_model)

    # Wrong volume → not a match → stays unverified.
    rows = [{"type": "courier", "acceptor_id": 7100, "volume": 1, "status": "finished",
             "contract_id": 7}]
    result = contracts_esi.reconcile_courier_contracts(client=_FakeESI(rows))

    assert result["verified"] == 0
    contract.refresh_from_db()
    assert contract.verification_state == "unverified"


@pytest.mark.django_db
def test_logistics_new_rule_targets_hauler_pool(db):
    """0.11: the seeded logistics.new rule is retargeted to the hauler pool (members)
    and its body uses the origin/destination the caller now populates."""
    from apps.pingboard.models import AutomationRule

    rule = AutomationRule.objects.get(key="logistics-new")
    assert rule.audience == {"kind": "role", "role": "member"}
    assert "{destination_system}" in rule.body
    assert "{origin_system}" in rule.body


@pytest.mark.django_db
def test_new_contract_fires_alert_with_real_destination(monkeypatch):
    """0.11: create_contract_from_quote fires logistics.new with the real destination
    (dest_name), origin and reward — the old code read a non-existent attr so
    {destination_system} always rendered empty."""
    from apps.logistics.pricing import quote
    from apps.logistics.services import create_contract_from_quote
    from apps.pingboard import hooks

    captured: dict = {}
    monkeypatch.setattr(hooks, "fire", lambda *a, **k: captured.update(k) or [])

    q = quote(_card(), ship_class="freighter", jumps=10, volume_m3=300_000,
              collateral=1_000_000_000, sec_band="highsec")
    create_contract_from_quote(
        quote=q, card=_card(), ship_class="freighter", volume_m3=300_000,
        collateral=1_000_000_000, rush=False,
        origin={"name": "Jita IV - Moon 4", "system_id": 30000142, "kind": "station", "id": 60003760},
        dest={"name": "Amarr VIII", "system_id": 30002187, "kind": "station", "id": 60008494},
    )
    ctx = captured.get("context", {})
    assert ctx.get("destination_system") == "Amarr VIII"
    assert ctx.get("origin_system") == "Jita IV - Moon 4"
    assert ctx.get("reward")  # formatted ISK string, non-empty
