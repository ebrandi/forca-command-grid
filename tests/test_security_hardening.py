"""Regression tests for the adversarial-QA security fixes."""
from __future__ import annotations

import time
from decimal import Decimal

import pytest
import responses

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001


def _member(django_user_model, username, *, corp_member=True):
    from apps.sso.models import EveCharacter

    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=int(username.split(":")[1]), user=user, name="P",
        is_main=True, is_corp_member=corp_member,
    )
    return user


# --- ReDoS / unbounded input (buyback appraisal) ----------------------------
def test_appraisal_parser_is_redos_safe_and_bounded():
    from apps.buyback.appraisal import parse_lines

    # The former catastrophic-backtracking vector: a long whitespace run. Must
    # return effectively instantly now.
    t = time.time()
    parse_lines(" " * 40000 + "a")
    assert time.time() - t < 1.0
    # Line count is capped so a huge paste can't fan out into unbounded DB work.
    assert len(parse_lines("\n".join(f"Item{i} 1" for i in range(2000)))) == 500


# --- Claim/buy race: atomic conditional transition --------------------------
@pytest.mark.django_db
def test_store_claim_is_atomic_and_blocks_self_fulfilment(client, django_user_model):
    from apps.store.models import Audience, StoreOrder
    from apps.store.services import active_config, invalidate_audience_cache

    cfg = active_config()
    cfg.audience = Audience.CORP
    cfg.save()
    invalidate_audience_cache()
    buyer = _member(django_user_model, "eve:6001")
    order = StoreOrder.objects.create(
        buyer=buyer, kind=StoreOrder.Kind.HULL, ship_type_id=587, ship_name="Rifter",
        total_price=Decimal("100"), status=StoreOrder.Status.OPEN,
    )
    # Buyer cannot fulfil their own order.
    client.force_login(buyer)
    client.post(f"/store/orders/{order.pk}/claim/")
    order.refresh_from_db()
    assert order.status == StoreOrder.Status.OPEN

    # First member wins the claim; a second claim on the now-CLAIMED order fails.
    m1 = _member(django_user_model, "eve:6002")
    client.force_login(m1)
    client.post(f"/store/orders/{order.pk}/claim/")
    order.refresh_from_db()
    assert order.status == StoreOrder.Status.CLAIMED and order.claimed_by_id == m1.id

    m2 = _member(django_user_model, "eve:6003")
    client.force_login(m2)
    client.post(f"/store/orders/{order.pk}/claim/")
    order.refresh_from_db()
    assert order.claimed_by_id == m1.id  # unchanged — second claimer didn't steal it


# --- Logistics state-machine guards -----------------------------------------
@pytest.mark.django_db
def test_logistics_transition_requires_in_progress(client, django_user_model):
    from apps.logistics.models import CourierContract

    m = _member(django_user_model, "eve:6100")
    contract = CourierContract.objects.create(
        origin_name="A", dest_name="B", status=CourierContract.Status.OUTSTANDING,
        assigned_user=m, reward=Decimal("1"),
    )
    client.force_login(m)
    # Cannot mark an OUTSTANDING (un-started) contract delivered.
    client.post(f"/freight/contracts/{contract.pk}/transition/", {"action": "delivered"})
    contract.refresh_from_db()
    assert contract.status == CourierContract.Status.OUTSTANDING


@pytest.mark.django_db
def test_logistics_cancel_guards_terminal_state(client, django_user_model):
    from apps.logistics.models import CourierContract

    officer = django_user_model.objects.create(username="eve:6101")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    contract = CourierContract.objects.create(
        origin_name="A", dest_name="B", status=CourierContract.Status.DELIVERED, reward=Decimal("1"),
    )
    client.force_login(officer)
    client.post(f"/freight/contracts/{contract.pk}/cancel/")
    contract.refresh_from_db()
    assert contract.status == CourierContract.Status.DELIVERED  # not cancellable


# --- Alliance FK population (makes the alliance feature actually work) -------
@responses.activate
@pytest.mark.django_db
def test_refresh_affiliation_populates_home_corp_alliance(settings):
    from apps.corporation.models import EveCorporation
    from apps.sso.models import EveCharacter
    from apps.sso.services import refresh_affiliation

    settings.FORCA_HOME_CORP_ID = HOME_CORP
    responses.add(
        responses.GET, "https://esi.evetech.net/characters/4242/",
        json={"corporation_id": HOME_CORP, "alliance_id": HOME_ALLIANCE}, status=200,
    )
    char = EveCharacter.objects.create(character_id=4242, name="Ally")
    refresh_affiliation(char)
    # The home corp's alliance FK is now set, so is_alliance checks can succeed.
    assert EveCorporation.objects.get(corporation_id=HOME_CORP).alliance_id == HOME_ALLIANCE


# --- Membership gate: whole-segment matching --------------------------------
def test_membership_gate_matches_whole_segments():
    from core.middleware import _path_allowed

    assert _path_allowed("/store") is True
    assert _path_allowed("/store/board/") is True
    assert _path_allowed("/freight/contracts/") is True
    assert _path_allowed("/auth/eve/login/") is True
    # A future root-mounted URL sharing a textual prefix must NOT be allowlisted.
    assert _path_allowed("/storeadmin/secrets") is False
    assert _path_allowed("/freightedanger") is False
    assert _path_allowed("/dashboard/") is False


# --- Officer config bounds enforced server-side -----------------------------
@pytest.mark.django_db
def test_config_forms_reject_out_of_range_values():
    from apps.buyback.forms import ConfigForm as BB
    from apps.logistics.forms import RateCardForm
    from apps.store.forms import ConfigForm as ST

    assert not ST(data={"name": "x", "audience": "alliance", "doctrine_markup": "0.5",
                         "hull_markup": "1.1", "deposit_pct": "0.25"}).is_valid()  # markup < 1
    assert not ST(data={"name": "x", "audience": "alliance", "doctrine_markup": "1.1",
                        "hull_markup": "1.1", "deposit_pct": "2"}).is_valid()  # deposit > 1
    assert not BB(data={"name": "x", "audience": "alliance", "highsec_pct": "1.5",
                        "lowsec_pct": "0.85", "nullsec_pct": "0.8"}).is_valid()  # pays > Jita
    assert not RateCardForm(data={"name": "x", "audience": "alliance", "discount": "0",
                                  "min_reward": "1"}).is_valid()  # zero multiplier → free


# --- JWT algorithm pinned to RS256 ------------------------------------------
def test_jwt_algorithm_pinned_to_rs256():
    import inspect

    from core.esi import oauth

    src = inspect.getsource(oauth.validate_access_token)
    assert '"RS256"' in src and "ES256" not in src


# --- JWT requires core claims and binds azp (round 4) -----------------------
def test_jwt_requires_core_claims_and_binds_azp():
    import inspect

    from core.esi import oauth

    src = inspect.getsource(oauth.validate_access_token)
    # A token missing exp/iss/sub/aud must be rejected, not silently accepted.
    assert '"require"' in src and '"exp"' in src
    # azp (authorized party) is bound to our client id.
    assert "azp" in src


def test_owner_hash_extracted_from_claims():
    from core.esi.oauth import owner_hash_from_claims

    assert owner_hash_from_claims({"owner": "abc123"}) == "abc123"
    assert owner_hash_from_claims({}) == ""


# --- Character-transfer account takeover refused (owner-hash) ----------------
@pytest.mark.django_db
def test_owner_hash_change_is_refused(django_user_model):
    from apps.sso.models import EveCharacter
    from apps.sso.services import CharacterOwnershipChanged, upsert_character

    user = django_user_model.objects.create(username="eve:7100")
    EveCharacter.objects.create(character_id=7100, user=user, name="P", owner_hash="HASH_A")
    # Same owner re-logging in: fine.
    upsert_character(user, 7100, "P", "HASH_A")
    # A changed owner hash (the character was sold/transferred) is refused rather
    # than inheriting this account — preventing takeover via an in-game transfer.
    with pytest.raises(CharacterOwnershipChanged):
        upsert_character(user, 7100, "P", "HASH_B")


# --- SRP separation-of-duties + state-machine guard -------------------------
@pytest.mark.django_db
def test_srp_self_approval_blocked_and_state_guarded(django_user_model):
    from decimal import Decimal as D

    from django.core.exceptions import PermissionDenied
    from django.utils import timezone

    from apps.killboard.models import Killmail
    from apps.srp import services as srp
    from apps.srp.models import SrpClaim

    officer = _member(django_user_model, "eve:7001")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    km = Killmail.objects.create(
        killmail_id=970001, killmail_time=timezone.now(), solar_system_id=30000142,
        victim_character_id=7001, victim_ship_type_id=587, total_value=D("1"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )
    claim = SrpClaim.objects.create(
        killmail=km, claimant=officer, status=SrpClaim.Status.SUBMITTED, computed_payout=D("100"),
    )
    # An officer may not approve or pay out their OWN claim.
    with pytest.raises(PermissionDenied):
        srp.decide(claim, officer, approve=True)
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.SUBMITTED
    with pytest.raises(PermissionDenied):
        srp.mark_paid(claim, officer)

    # A different officer can decide it.
    other = _member(django_user_model, "eve:7002")
    RoleAssignment.objects.create(user=other, role=ensure_role(rbac.ROLE_OFFICER))
    assert srp.decide(claim, other, approve=True) is True
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.APPROVED

    # State guard: an already-decided claim cannot be re-decided out of state.
    assert srp.decide(claim, other, approve=False) is False
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.APPROVED

    # Pay once; a second pay is a no-op (no double credit / re-open).
    assert srp.mark_paid(claim, other) is True
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.PAID
    assert srp.mark_paid(claim, other) is False


# --- Director role withdrawn once it can no longer be proven -----------------
@pytest.mark.django_db
def test_director_withdrawn_when_no_proving_scope(django_user_model, monkeypatch):
    from apps.sso import services as sso

    user = _member(django_user_model, "eve:7200")  # corp member char 7200
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    # ESI can't answer (no token). With NO active proving scope grant, the stale
    # Director grant must be withdrawn — it can't outlive the token that proved it.
    monkeypatch.setattr(sso, "character_is_corp_director", lambda c, client=None: None)
    sso.sync_roles_for_user(user)
    assert not rbac.has_role(user, rbac.ROLE_DIRECTOR)


@pytest.mark.django_db
def test_director_kept_on_transient_esi_failure_with_token(django_user_model, monkeypatch):
    from django.utils import timezone as tz

    from apps.sso import services as sso
    from apps.sso.models import AuthToken

    user = _member(django_user_model, "eve:7201")
    char = user.characters.first()
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    token = AuthToken(
        character=char, scopes=[sso.ROLE_SCOPE],
        access_expires_at=tz.now() + tz.timedelta(hours=1),
    )
    token.refresh_token = "r"
    token.access_token = "a"
    token.save()
    # ESI is briefly down (None) but a proving token still exists → keep the role
    # rather than flapping it off on every transient failure.
    monkeypatch.setattr(sso, "character_is_corp_director", lambda c, client=None: None)
    sso.sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR)


# --- Token-encryption fallback requires explicit opt-in (not DEBUG) ---------
def test_token_key_fallback_requires_explicit_flag(settings):
    from django.core.exceptions import ImproperlyConfigured

    from core.esi import tokens

    settings.TOKEN_ENCRYPTION_KEY = "not-a-valid-fernet-key"
    settings.ALLOW_DERIVED_TOKEN_KEY = False
    with pytest.raises(ImproperlyConfigured):
        tokens.get_fernet()
    # Only the explicit opt-in (never DEBUG alone) enables the derived dev key.
    settings.ALLOW_DERIVED_TOKEN_KEY = True
    assert tokens.get_fernet().encrypt(b"x")


# --- KB renderer keeps generated markup out of attribute values -------------
def test_kb_renderer_no_raw_markup_in_href():
    from apps.kb.render import render_markdown

    # "**" inside a URL must NOT be turned into <strong> inside the href value:
    # the link is tokenized before the bold/italic/code passes run.
    out = str(render_markdown("[t](https://a.com/**x**)"))
    assert "<strong>" not in out
    assert 'href="https://a.com/**x**"' in out
    # Bold still works in ordinary text.
    assert "<strong>y</strong>" in str(render_markdown("**y**"))
    # javascript: links remain inert (not linkified).
    assert "<a" not in str(render_markdown("[x](javascript:alert(1))"))


# --- Discord webhook egress: scheme + host + path all required --------------
def test_discord_webhook_requires_webhook_path(monkeypatch):
    from apps.recommendations import notify

    calls = []
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(k.get("json")))
    # Allowlisted host but a non-webhook path → refused (no request made).
    notify._post_discord("https://discord.com/evil", "hi")
    assert calls == []
    # Internal/metadata host → refused even with a webhook-looking path.
    notify._post_discord("https://169.254.169.254/api/webhooks/1/x", "hi")
    assert calls == []
    # Genuine Discord webhook URL → posted.
    notify._post_discord("https://discord.com/api/webhooks/1/abc", "hi")
    assert len(calls) == 1


# --- ESI base URL is pinned to an allowlist (token-egress guard) -------------
def test_esi_base_url_is_allowlisted():
    from urllib.parse import urlparse

    from django.conf import settings

    host = urlparse(settings.ESI_BASE_URL).hostname
    assert host in {"esi.evetech.net", "localhost", "127.0.0.1"}
