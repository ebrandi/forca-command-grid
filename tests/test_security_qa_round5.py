"""Regression tests for the round-5 adversarial-QA fixes:

- navigation token amplification cap (unauthenticated DoS),
- mining ``line_paid`` FINAL-state lock,
- Discord webhook mass-mention / redirect-follow / length hardening,
- buyback pasted-quantity overflow guard,
- legacy empty-``owner_hash`` account-takeover guard,
- ``character_id_from_claims`` non-numeric sub handling.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, name, role):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


# --- Navigation token amplification -----------------------------------------
def test_split_caps_token_count():
    from apps.navigation.services import _MAX_TOKENS, _split

    assert _split(",".join(["Jita"] * 5000)) == ["Jita"] * _MAX_TOKENS
    assert _split("Jita, Amarr , Dodixie") == ["Jita", "Amarr", "Dodixie"]
    assert _split("") == []


# --- Mining payout: finalised lines are frozen ------------------------------
@pytest.mark.django_db
def test_line_paid_rejected_on_finalised_payout(client, django_user_model):
    from apps.mining.models import MiningPayout, MiningPayoutLine

    today = dt.date.today()
    payout = MiningPayout.objects.create(
        name="P", period_start=today, period_end=today, status=MiningPayout.Status.FINAL,
    )
    line = MiningPayoutLine.objects.create(payout=payout, character_id=1, paid=False)
    client.force_login(_user(django_user_model, "eve:r5a", rbac.ROLE_OFFICER))
    resp = client.post(f"/mining/payouts/{payout.pk}/lines/{line.id}/paid/")
    assert resp.status_code == 302
    line.refresh_from_db()
    assert line.paid is False  # frozen — the toggle was rejected


@pytest.mark.django_db
def test_line_paid_toggles_on_draft_payout(client, django_user_model):
    from apps.mining.models import MiningPayout, MiningPayoutLine

    today = dt.date.today()
    payout = MiningPayout.objects.create(
        name="P", period_start=today, period_end=today, status=MiningPayout.Status.DRAFT,
    )
    line = MiningPayoutLine.objects.create(payout=payout, character_id=1, user=None, paid=False)
    client.force_login(_user(django_user_model, "eve:r5b", rbac.ROLE_OFFICER))
    client.post(f"/mining/payouts/{payout.pk}/lines/{line.id}/paid/")
    line.refresh_from_db()
    assert line.paid is True


# --- Discord webhook hardening ----------------------------------------------
def test_post_discord_neutralises_mentions_and_redirects(monkeypatch):
    from apps.recommendations import notify

    captured = {}

    def fake_post(url, json=None, timeout=None, allow_redirects=None):
        captured.update(url=url, json=json, allow_redirects=allow_redirects)

        class _R:
            status_code = 204
        return _R()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify._post_discord(
        "https://discord.com/api/webhooks/1/abc", "@everyone " + "x" * 5000
    )
    assert captured["allow_redirects"] is False
    assert captured["json"]["allowed_mentions"] == {"parse": []}
    assert len(captured["json"]["content"]) <= 2000


def test_post_discord_refuses_non_discord_host(monkeypatch):
    from apps.recommendations import notify

    called = {"n": 0}
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: called.__setitem__("n", 1))
    notify._post_discord("https://evil.example/api/webhooks/1/abc", "hi")
    assert called["n"] == 0  # SSRF guard blocked it before any request


# --- Buyback pasted-quantity overflow ---------------------------------------
def test_to_int_caps_absurd_quantity():
    from apps.buyback.appraisal import _MAX_QTY, _to_int

    assert _to_int("9" * 60) == _MAX_QTY      # 60-digit paste clamped
    assert _to_int("100") == 100
    assert _to_int("x") is None


# --- Account takeover: legacy empty owner_hash ------------------------------
@pytest.mark.django_db
def test_legacy_empty_owner_hash_fails_closed(django_user_model):
    from apps.sso.models import EveCharacter
    from apps.sso.services import CharacterOwnershipChanged, upsert_character

    user = django_user_model.objects.create(username="eve:r5c")
    EveCharacter.objects.create(character_id=42, user=user, name="Legacy", owner_hash="")
    # A login presenting a real owner hash against a linked row with no stored hash
    # cannot prove same-owner → must be refused, not silently inherit the account.
    with pytest.raises(CharacterOwnershipChanged):
        upsert_character(user, 42, "Legacy", owner_hash="fresh-owner-hash")


@pytest.mark.django_db
def test_unlinked_character_claims_owner_hash_freely(django_user_model):
    from apps.sso.models import EveCharacter
    from apps.sso.services import upsert_character

    user = django_user_model.objects.create(username="eve:r5d")
    # An unowned row is a fresh claim — it should bind and record the owner hash.
    EveCharacter.objects.create(character_id=43, user=None, name="New", owner_hash="")
    char = upsert_character(user, 43, "New", owner_hash="brand-new-hash")
    assert char.owner_hash == "brand-new-hash"
    assert char.user_id == user.id


# --- JWT sub parsing --------------------------------------------------------
def test_character_id_from_claims_rejects_non_numeric():
    from core.esi.oauth import JWTValidationError, character_id_from_claims

    with pytest.raises(JWTValidationError):
        character_id_from_claims({"sub": "CHARACTER:EVE:not-a-number"})
    assert character_id_from_claims({"sub": "CHARACTER:EVE:90000001"}) == 90000001
