"""4.7 — CCP-authoritative scope reconciliation.

Acceptance: our EveScopeGrant rows track what CCP *actually* still honours. A scope
whose only token was revoked deactivates; a scope a live token still carries stays
(or is re-)activated; active verification reads the CCP `scp` claim and corrects a
drifted stored copy; a failed verification never wipes grants; the corp-wide sweep is
staleness-filtered + per-run capped; the self-service button reconciles only the
caller's own characters.
"""
from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.sso import reconcile as R
from apps.sso.models import AuthToken, EveScopeGrant
from apps.sso.token_service import NoValidToken
from tests._raffle_utils import add_token, enrol_pilot

pytestmark = pytest.mark.django_db

A = "esi-skills.read_skills.v1"
B = "esi-assets.read_assets.v1"


def _grant(character, scope, *, active=True):
    return EveScopeGrant.objects.create(character=character, scope=scope, active=active)


def _patch_ccp(monkeypatch, scp_by_token=None, *, raise_for=()):
    """Make active verification deterministic. ``scp_by_token`` maps token.pk -> scope
    list CCP reports; ``raise_for`` is a set of token.pks whose refresh 'fails'."""
    scp_by_token = scp_by_token or {}

    def fake_access(token):
        if token.pk in raise_for:
            raise NoValidToken("dead")
        return f"access-{token.pk}"

    def fake_validate(access):
        pk = int(access.rsplit("-", 1)[1])
        return {"scp": scp_by_token.get(pk, [])}

    monkeypatch.setattr(R, "access_token_for", fake_access)
    monkeypatch.setattr(R.oauth, "validate_access_token", fake_validate)


def test_passive_deactivates_scope_with_no_live_token(django_user_model):
    _u, char = enrol_pilot(django_user_model, 2001, scopes=[A])  # one live token carrying A
    _grant(char, A, active=True)
    _grant(char, B, active=True)  # B has no token backing it any more
    res = R.reconcile_character_scopes(char, verify=False)
    assert res["deactivated"] == [B]
    assert EveScopeGrant.objects.get(character=char, scope=A).active is True
    assert EveScopeGrant.objects.get(character=char, scope=B).active is False


def test_passive_reactivates_scope_a_live_token_still_carries(django_user_model):
    _u, char = enrol_pilot(django_user_model, 2002, scopes=[A])
    _grant(char, A, active=False)  # stale-inactive, but a live token has A
    res = R.reconcile_character_scopes(char, verify=False)
    assert res["activated"] == [A]
    assert EveScopeGrant.objects.get(character=char, scope=A).active is True


def test_passive_creates_missing_grant_row(django_user_model):
    _u, char = enrol_pilot(django_user_model, 2003, scopes=[A, B])  # token has both, no grant rows
    res = R.reconcile_character_scopes(char, verify=False)
    assert set(res["activated"]) == {A, B}
    assert EveScopeGrant.objects.filter(character=char, active=True).count() == 2


def test_active_verify_corrects_ccp_drift(django_user_model, monkeypatch):
    _u, char = enrol_pilot(django_user_model, 2004, scopes=[A, B])
    token = AuthToken.objects.get(character=char)
    _grant(char, A, active=True)
    _grant(char, B, active=True)
    # CCP now only honours A (pilot pared the grant back at CCP).
    _patch_ccp(monkeypatch, {token.pk: [A]})
    res = R.reconcile_character_scopes(char, verify=True)
    assert res["verified"] == 1
    assert res["deactivated"] == [B]
    token.refresh_from_db()
    assert token.scopes == [A]                      # stored copy corrected to CCP truth
    assert token.scopes_verified_at is not None     # stamped
    assert EveScopeGrant.objects.get(character=char, scope=B).active is False


def test_failed_verification_falls_back_and_keeps_grants(django_user_model, monkeypatch):
    _u, char = enrol_pilot(django_user_model, 2005, scopes=[A])
    token = AuthToken.objects.get(character=char)
    _grant(char, A, active=True)
    _patch_ccp(monkeypatch, raise_for={token.pk})   # verification can't reach CCP
    res = R.reconcile_character_scopes(char, verify=True)
    assert res["verified"] == 0
    # Falls back to the recorded scopes; the grant is NOT wrongly deactivated.
    assert EveScopeGrant.objects.get(character=char, scope=A).active is True


def test_revoked_token_scope_deactivates(django_user_model):
    _u, char = enrol_pilot(django_user_model, 2006, scopes=[A])
    live = AuthToken.objects.get(character=char)
    second = add_token(char, scopes=[B])            # a second live token carrying B
    _grant(char, A, active=True)
    _grant(char, B, active=True)
    # Revoke the B-bearing token → B no longer honoured.
    second.revoked_at = timezone.now()
    second.save(update_fields=["revoked_at"])
    R.reconcile_character_scopes(char, verify=False)
    assert EveScopeGrant.objects.get(character=char, scope=A).active is True
    assert EveScopeGrant.objects.get(character=char, scope=B).active is False
    assert live.revoked_at is None


def test_batch_is_staleness_filtered_and_capped(django_user_model, monkeypatch):
    # Fresh char: token verified just now → skipped by the staleness cutoff.
    _u1, fresh = enrol_pilot(django_user_model, 2101, scopes=[A])
    AuthToken.objects.filter(character=fresh).update(scopes_verified_at=timezone.now())
    # Two never-verified chars → both due.
    _u2, stale1 = enrol_pilot(django_user_model, 2102, scopes=[A])
    _u3, stale2 = enrol_pilot(django_user_model, 2103, scopes=[A])
    _patch_ccp(
        monkeypatch,
        {t.pk: [A] for t in AuthToken.objects.all()},
    )
    res = R.reconcile_scopes_batch(limit=1, staleness_hours=24, verify=True)
    assert res["checked"] == 1          # capped to one per run
    # The fresh character was never touched.
    assert AuthToken.objects.get(character=fresh).scopes_verified_at is not None


def test_self_service_view_reconciles_own_characters(client, django_user_model, monkeypatch):
    user, char = enrol_pilot(django_user_model, 2201, scopes=[A])
    token = AuthToken.objects.get(character=char)
    _grant(char, A, active=True)
    _grant(char, B, active=True)         # B is stale — CCP won't confirm it
    _patch_ccp(monkeypatch, {token.pk: [A]})
    client.force_login(user)
    resp = client.post(reverse("sso:reconcile"))
    assert resp.status_code == 302
    assert EveScopeGrant.objects.get(character=char, scope=B).active is False
    assert EveScopeGrant.objects.get(character=char, scope=A).active is True


def test_scopes_page_shows_recheck_button(client, django_user_model):
    user, _char = enrol_pilot(django_user_model, 2202, scopes=[A])
    client.force_login(user)
    resp = client.get(reverse("sso:scopes"))
    assert resp.status_code == 200
    assert b"Re-check with CCP" in resp.content
