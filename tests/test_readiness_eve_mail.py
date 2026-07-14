"""Gap D — EVE-mail outbound: configurable director sender + alert delivery.

Sending uses the chosen sender's ``esi-mail.send_mail.v1`` token against ESI
``POST /characters/{id}/mail/``; best-effort, gated to high/critical alerts with a
mapped owner. With no sender/token it degrades to a no-op (the historic behaviour).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness import mail as rmail
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import rbac

SEND_SCOPE = "esi-mail.send_mail.v1"


def _director_char(django_user_model, cid=2001, name="Sender", granted=True):
    user = django_user_model.objects.create(username=f"u{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    ch = EveCharacter.objects.create(character_id=cid, name=name, is_main=True,
                                     is_corp_member=True, user=user)
    if granted:
        AuthToken.objects.create(character=ch, scopes=[SEND_SCOPE])
    return user, ch


# --- validator ---------------------------------------------------------------
@pytest.mark.django_db
def test_notifications_validator_coerces_and_rejects():
    assert config.set("notifications", {"eve_mail_sender_character_id": "2001"})[
        "eve_mail_sender_character_id"] == 2001
    assert config.set("notifications", {"eve_mail_sender_character_id": ""})[
        "eve_mail_sender_character_id"] is None
    with pytest.raises(config.ConfigError):
        config.set("notifications", {"eve_mail_sender_character_id": "abc"})


# --- eligible senders + sender lookup ---------------------------------------
@pytest.mark.django_db
def test_eligible_senders_lists_directors_with_grant_status(django_user_model):
    _director_char(django_user_model, 2001, "Granted", granted=True)
    _director_char(django_user_model, 2002, "NotGranted", granted=False)
    # a non-director member is excluded
    m = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=m, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=2003, name="Member", is_main=True,
                                is_corp_member=True, user=m)
    rows = {r["name"]: r["granted"] for r in rmail.eligible_senders()}
    assert rows == {"Granted": True, "NotGranted": False}


@pytest.mark.django_db
def test_configured_sender_resolves(django_user_model):
    _director_char(django_user_model, 2001)
    config.set("notifications", {"eve_mail_sender_character_id": 2001})
    assert rmail.eve_mail_sender().character_id == 2001


# --- recipient resolution ----------------------------------------------------
@pytest.mark.django_db
def test_owner_recipient_ids_resolves_user_mains(django_user_model):
    officer = django_user_model.objects.create(username="off")
    EveCharacter.objects.create(character_id=3001, name="OffMain", is_main=True,
                                is_corp_member=True, user=officer)
    resp = {"owner_tags": {"finance_officer": {"label": "Finance", "users": [officer.id]}}}
    assert rmail.owner_recipient_ids("finance_officer", resp) == [3001]
    assert rmail.owner_recipient_ids("", resp) == []
    assert rmail.owner_recipient_ids("nobody", resp) == []


# --- send_mail (mocked ESI) --------------------------------------------------
@pytest.mark.django_db
def test_send_mail_no_sender_is_noop(django_user_model):
    assert rmail.send_mail("s", "b", [3001]) is False  # nothing configured


@pytest.mark.django_db
def test_send_mail_posts_to_esi(django_user_model, monkeypatch):
    _director_char(django_user_model, 2001)
    config.set("notifications", {"eve_mail_sender_character_id": 2001})

    calls = {}

    class _FakeClient:
        def post(self, path, *, json=None, token=None):
            calls["path"] = path
            calls["json"] = json
            calls["token"] = token
            return SimpleNamespace(status=201, data=42)

    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, scopes: "tok")
    monkeypatch.setattr("core.esi.client.get_client", lambda: _FakeClient())

    assert rmail.send_mail("Subject", "Body", [3001, 3002]) is True
    assert calls["path"] == "/characters/2001/mail/"
    assert calls["token"] == "tok"
    assert calls["json"]["recipients"] == [
        {"recipient_id": 3001, "recipient_type": "character"},
        {"recipient_id": 3002, "recipient_type": "character"},
    ]


@pytest.mark.django_db
def test_send_mail_swallows_esi_error(django_user_model, monkeypatch):
    _director_char(django_user_model, 2001)
    config.set("notifications", {"eve_mail_sender_character_id": 2001})
    from core.esi.client import ESIError

    class _BoomClient:
        def post(self, *a, **k):
            raise ESIError("nope")

    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, scopes: "tok")
    monkeypatch.setattr("core.esi.client.get_client", lambda: _BoomClient())
    assert rmail.send_mail("s", "b", [3001]) is False  # error → no-op, no raise


# --- alerts._deliver gating --------------------------------------------------
def _finding(**kw):
    base = {"dimension_key": "financial", "kpi_key": "financial.runway_months",
            "title": "Low runway", "owner_tag": "finance_officer", "score": 20}
    base.update(kw)
    # Seam B: the alert/mail renderers read ``title_i18n`` (the finding's title resolved under the
    # READER's locale). This stand-in has no scaffold key — like every legacy row — so, exactly as
    # the real property does, it renders the stored English verbatim.
    base.setdefault("title_i18n", base["title"])
    return SimpleNamespace(**base)


def test_deliver_sends_eve_mail_for_high_with_owner(monkeypatch):
    from apps.readiness import alerts

    sent = {}
    monkeypatch.setattr("apps.readiness.mail.send_mail",
                        lambda subj, body, rec: sent.update(subj=subj, body=body, rec=rec) or True)
    monkeypatch.setattr("apps.readiness.mail.owner_recipient_ids", lambda tag, resp: [3001])
    resp = {"owner_tags": {"finance_officer": {"label": "Finance Desk", "users": [1]}}}
    rule = {"key": "r", "severity": "high"}
    delivered = alerts._deliver("msg", ["eve_mail"], rule=rule, finding=_finding(), responsibilities=resp)
    assert "eve_mail" in delivered
    assert sent["rec"] == [3001]
    assert "FORCA Readiness" in sent["subj"]


def test_deliver_skips_eve_mail_for_warn(monkeypatch):
    from apps.readiness import alerts

    called = []

    def _spy(*a, **k):
        called.append(1)
        return True

    monkeypatch.setattr("apps.readiness.mail.send_mail", _spy)
    monkeypatch.setattr("apps.readiness.mail.owner_recipient_ids", lambda tag, resp: [3001])
    rule = {"key": "r", "severity": "warn"}
    delivered = alerts._deliver("msg", ["eve_mail"], rule=rule, finding=_finding(), responsibilities={})
    assert "eve_mail" not in delivered
    assert called == []  # warn never attempts EVE-mail (doc 13 §EVE-mail)


# --- admin sender save -------------------------------------------------------
@pytest.mark.django_db
def test_admin_saves_sender(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    _, ch = _director_char(django_user_model, 2001)
    director = django_user_model.objects.create(username="dir2")
    RoleAssignment.objects.create(user=director, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(director)
    resp = client.post("/ops/admin/readiness/alerts/sender/", {"sender_character_id": "2001"})
    assert resp.status_code == 302
    assert config.get("notifications")["eve_mail_sender_character_id"] == 2001
    assert AuditLog.objects.filter(action="readiness.config.update", target_id="notifications").exists()
    # The alerts page renders the sender selector.
    assert "EVE-mail sender" in client.get("/ops/admin/readiness/alerts/").content.decode()
