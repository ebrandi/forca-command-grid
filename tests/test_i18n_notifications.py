"""Per-recipient notification & email localisation (design doc 08, acceptance 16/17).

Proves the re-render seam end to end WITHOUT depending on a shipped ``.mo`` catalogue:
a temporary translation is injected into the active locale's in-memory catalog (the
scaffold msgids here are test-only strings), and a code scaffold is registered in
``messages.SCAFFOLDS`` for the duration of the test. Both are torn down after.

Coverage:
* emit persists ``Alert.template_key`` + ``Alert.context``; ``Alert.body`` stays the
  default-locale render.
* ``_bucket_by_language`` groups per-user recipients by ``user.language`` deterministically;
  blank/disabled locales collapse to the broadcast locale.
* Two recipients with different ``user.language`` get bodies rendered in their OWN language
  from the SAME emit, with the interpolated EVE var kept RAW in every locale.
* A broadcast (Discord) leg renders once in ``broadcast_locale`` and records it.
* A ``custom_message`` alert passes through VERBATIM in every locale.
* Per-pilot delivery language is recorded on ``AlertRecipient``.
"""
from __future__ import annotations

import pytest
from django.utils import translation
from django.utils.translation import gettext_lazy, trans_real

from apps.identity.models import RoleAssignment
from apps.pingboard import messages, services
from apps.pingboard.dispatch import AlertDispatcher
from apps.pingboard.models import AlertDelivery, AlertRecipient, ChannelProvider
from apps.pingboard.providers import Recipient, SendResult
from apps.pingboard.providers.discord import DiscordProvider
from apps.pingboard.providers.evemail import EveMailProvider
from apps.pingboard.rendering_i18n import render_for
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from core.i18n import config as i18n_config

pytestmark = pytest.mark.django_db

_KEY = "test.formup_reminder"
_SUBJECT_MSGID = "Form-up reminder"
_BODY_MSGID = "Fleet forms up at {formup_system} in {start_time} — see you there."
_PT = {
    _SUBJECT_MSGID: "Lembrete de formação",
    _BODY_MSGID: "A frota forma em {formup_system} em {start_time} — até lá.",
}
_CTX = {"formup_system": "Jita", "start_time": "15 min", "link": "comms-1"}


@pytest.fixture
def pt_scaffold():
    """Enable pt-br, register a gettext-wrapped scaffold, and inject a pt-br catalogue.

    Yields the injected translation object; tears the scaffold + injected msgids down so
    the module-level registry and the process-wide catalogue are not polluted.
    """
    i18n_config.set_i18n_config(locales={"pt-br": True})
    messages.SCAFFOLDS[_KEY] = messages.Scaffold(
        subject=gettext_lazy(_SUBJECT_MSGID), body=gettext_lazy(_BODY_MSGID)
    )
    t = trans_real.translation("pt-br")
    for msgid, msgstr in _PT.items():
        t._catalog[msgid] = msgstr
    try:
        yield t
    finally:
        messages.SCAFFOLDS.pop(_KEY, None)
        # Drop the cached translation so the injected msgids never leak into another test
        # (the catalogue rebuilds from the shipped .mo on next use).
        trans_real._translations.pop("pt-br", None)


def _member(django_user_model, cid, language=""):
    u = django_user_model.objects.create(username=f"eve:{cid}", language=language)
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=cid, user=u, name=f"P{cid}", is_main=True, is_corp_member=True
    )
    return u


def _eve_mail_provider():
    return ChannelProvider.objects.create(
        kind="eve_mail", label="sender", enabled=True,
        routing={"sender_character_id": 9999}, supports_direct=True,
    )


def _record_sends(monkeypatch, provider_cls):
    """Capture every provider.send call (subject/body/active-locale/recipients)."""
    sent: list[dict] = []

    def fake_send(self, *, subject, body, recipients):
        sent.append({
            "subject": subject, "body": body,
            "lang": translation.get_language(),
            "user_ids": sorted(r.user_id for r in recipients if r.user_id),
        })
        return SendResult(ok=True, recipients_ok=len(recipients))

    monkeypatch.setattr(provider_cls, "send", fake_send)
    return sent


# --- 1. emit persists the re-render inputs -----------------------------------
def test_emit_persists_template_key_and_context(pt_scaffold):
    alert = services.emit_alert(
        category="pvp_fleet", title="Form up", template=_KEY, context=_CTX,
        channels=["eve_mail"], audience={"kind": "corp"}, source_service="ops",
        bypass_ratelimit=True,
    )
    assert alert is not None
    assert alert.template_key == _KEY
    assert alert.context == {"formup_system": "Jita", "start_time": "15 min", "link": "comms-1"}
    assert alert.custom_message is False
    # Alert.body is the frozen DEFAULT-locale (English) render — the audit record.
    assert alert.body == "Fleet forms up at Jita in 15 min — see you there."


# --- 2. bucket-by-language ----------------------------------------------------
def test_bucket_by_language_is_deterministic(django_user_model, pt_scaffold):
    en_user = _member(django_user_model, 5001, language="en")
    pt_user = _member(django_user_model, 5002, language="pt-br")
    blank_user = _member(django_user_model, 5003, language="")  # → broadcast default (en)

    recips = [
        Recipient("eve_mail", "character", "5001", en_user.id),
        Recipient("eve_mail", "character", "5002", pt_user.id),
        Recipient("eve_mail", "character", "5003", blank_user.id),
    ]
    buckets = AlertDispatcher()._bucket_by_language(recips)

    assert list(buckets.keys()) == ["en", "pt-br"]  # sorted; blank merged into en
    assert {r.user_id for r in buckets["en"]} == {en_user.id, blank_user.id}
    assert {r.user_id for r in buckets["pt-br"]} == {pt_user.id}


# --- 3. render_for per locale, raw EVE var ------------------------------------
def test_render_for_translates_scaffold_keeps_var_raw(pt_scaffold):
    alert = services.emit_alert(
        category="pvp_fleet", title="Form up", template=_KEY, context=_CTX,
        channels=["eve_mail"], audience={"kind": "corp"}, source_service="ops",
        bypass_ratelimit=True,
    )
    with translation.override("en"):
        _, body_en = render_for(alert, "en")
    with translation.override("pt-br"):
        subj_pt, body_pt = render_for(alert, "pt-br")

    assert body_en == "Fleet forms up at Jita in 15 min — see you there."
    assert body_pt == "A frota forma em Jita em 15 min — até lá."
    assert subj_pt == "Lembrete de formação"
    # The interpolated EVE system name stays RAW in every locale (never translated).
    assert "Jita" in body_en and "Jita" in body_pt


# --- 4. two recipients, one emit, each their own language ---------------------
def test_two_recipients_get_own_language_from_same_emit(django_user_model, monkeypatch, pt_scaffold):
    _member(django_user_model, 6001, language="en")
    _member(django_user_model, 6002, language="pt-br")
    _eve_mail_provider()
    sent = _record_sends(monkeypatch, EveMailProvider)

    alert = services.emit_alert(
        category="pvp_fleet", title="Form up", template=_KEY, context=_CTX,
        channels=["eve_mail"], audience={"kind": "corp"}, source_service="ops",
        bypass_ratelimit=True,
    )
    services.dispatch_alert(alert.id)

    by_lang = {s["lang"]: s for s in sent}
    assert set(by_lang) == {"en", "pt-br"}  # one send per locale bucket
    assert by_lang["en"]["body"] == "Fleet forms up at Jita in 15 min — see you there."
    assert by_lang["pt-br"]["body"] == "A frota forma em Jita em 15 min — até lá."
    # EVE var raw in both; the pilots were bucketed to their own send.
    assert "Jita" in by_lang["en"]["body"] and "Jita" in by_lang["pt-br"]["body"]


# --- 5. broadcast uses broadcast_locale --------------------------------------
def test_broadcast_leg_uses_broadcast_locale(django_user_model, monkeypatch, pt_scaffold):
    # No single recipient on a Discord webhook → render once in the corp broadcast locale.
    i18n_config.set_i18n_config(locales={"pt-br": True}, broadcast_locale="pt-br")
    _member(django_user_model, 7001, language="en")  # audience member, but Discord is broadcast
    ChannelProvider.objects.create(kind="discord", label="corp", enabled=True, supports_channel=True)
    sent = _record_sends(monkeypatch, DiscordProvider)

    alert = services.emit_alert(
        category="pvp_fleet", title="Form up", template=_KEY, context=_CTX,
        channels=["discord"], audience={"kind": "corp"}, source_service="ops",
        bypass_ratelimit=True,
    )
    services.dispatch_alert(alert.id)

    assert len(sent) == 1
    assert sent[0]["lang"] == "pt-br"
    assert sent[0]["body"] == "A frota forma em Jita em 15 min — até lá."
    # And the delivery row records the broadcast locale it rendered in.
    d = AlertDelivery.objects.get(alert=alert, kind="discord")
    assert d.language == "pt-br"


# --- 6. custom free-text passes through verbatim -----------------------------
def test_custom_message_is_verbatim_in_every_locale(django_user_model, monkeypatch, pt_scaffold):
    _member(django_user_model, 8001, language="en")
    _member(django_user_model, 8002, language="pt-br")
    _eve_mail_provider()
    sent = _record_sends(monkeypatch, EveMailProvider)

    alert = services.emit_alert(
        category="pvp_fleet", title="Hostiles", body="Undock now — hostiles in home.",
        channels=["eve_mail"], audience={"kind": "corp"}, source_service="ops",
        bypass_ratelimit=True,
    )
    assert alert.custom_message is True
    services.dispatch_alert(alert.id)

    bodies = {s["body"] for s in sent}
    assert bodies == {"Undock now — hostiles in home."}  # identical across both buckets
    assert all(s["body"] == alert.body for s in sent)     # == the frozen officer text


# --- 7. per-pilot delivery language is recorded ------------------------------
def test_delivery_language_recorded_per_recipient(django_user_model, monkeypatch, pt_scaffold):
    en_user = _member(django_user_model, 9001, language="en")
    pt_user = _member(django_user_model, 9002, language="pt-br")
    _eve_mail_provider()
    _record_sends(monkeypatch, EveMailProvider)

    alert = services.emit_alert(
        category="pvp_fleet", title="Form up", template=_KEY, context=_CTX,
        channels=["eve_mail"], audience={"kind": "corp"}, source_service="ops",
        bypass_ratelimit=True,
    )
    services.dispatch_alert(alert.id)

    assert AlertRecipient.objects.get(alert=alert, user=en_user).language == "en"
    assert AlertRecipient.objects.get(alert=alert, user=pt_user).language == "pt-br"
