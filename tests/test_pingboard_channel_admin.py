"""Guided channel add / edit in the Admin Console (Director-gated)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.pingboard.models import ChannelProvider
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

_WEBHOOK = "https://discord.com/api/webhooks/42/DIF-SECRET-TOKEN-XYZ"


def _director(django_user_model, uid=9100):
    u = django_user_model.objects.create(username=f"u{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    EveCharacter.objects.create(character_id=uid, user=u, name=f"Dir{uid}",
                                is_main=True, is_corp_member=True)
    return u


# --- add form ----------------------------------------------------------------
@pytest.mark.django_db
def test_new_form_renders_for_director(client, django_user_model):
    client.force_login(_director(django_user_model))
    resp = client.get(reverse("admin_audit:pingboard_channel_new"))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Add a channel" in html
    # adaptive per-kind fields are present in the DOM (Alpine toggles visibility)
    assert 'name="routing_chat_id"' in html and 'name="routing_to"' in html
    assert 'name="routing_sender_character_id"' in html
    # Telegram token + WhatsApp backend/creds are configurable here (no env needed)
    assert 'name="secret"' in html  # bot token / access token field
    assert 'name="routing_backend"' in html
    assert 'name="routing_meta_phone_id"' in html and 'name="routing_twilio_sid"' in html


@pytest.mark.django_db
def test_officer_cannot_reach_channel_form(client, django_user_model):
    u = django_user_model.objects.create(username="off1")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(u)
    assert client.get(reverse("admin_audit:pingboard_channel_new")).status_code == 403
    assert client.post(reverse("admin_audit:pingboard_channel_save"),
                       {"kind": "discord", "label": "x"}).status_code == 403


# --- create via the guided save ----------------------------------------------
@pytest.mark.django_db
def test_save_creates_discord_and_sets_capabilities(client, django_user_model):
    client.force_login(_director(django_user_model))
    resp = client.post(reverse("admin_audit:pingboard_channel_save"), {
        "kind": "discord", "label": "Corp #alerts", "secret": _WEBHOOK, "enabled": "on",
    })
    assert resp.status_code == 302
    prov = ChannelProvider.objects.get(kind="discord")
    assert prov.label == "Corp #alerts" and prov.enabled and prov.has_secret
    assert prov.secret == _WEBHOOK
    # capability flags come from the provider class, not the form
    assert prov.supports_channel is True


@pytest.mark.django_db
def test_save_telegram_keeps_only_its_routing_key(client, django_user_model):
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:pingboard_channel_save"), {
        "kind": "telegram", "label": "TG", "routing_chat_id": "-1001234567890",
        "routing_to": "+15550000000",  # wrong key for telegram — must be ignored
    })
    prov = ChannelProvider.objects.get(kind="telegram")
    assert prov.routing == {"chat_id": "-1001234567890"}


@pytest.mark.django_db
def test_save_telegram_stores_bot_token_as_secret(client, django_user_model):
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:pingboard_channel_save"), {
        "kind": "telegram", "label": "TG", "secret": "123456:BOT-TOKEN-XYZ",
        "routing_chat_id": "-100999", "enabled": "on",
    })
    prov = ChannelProvider.objects.get(kind="telegram")
    assert prov.has_secret and prov.secret == "123456:BOT-TOKEN-XYZ"
    assert prov.routing == {"chat_id": "-100999"} and prov.enabled


@pytest.mark.django_db
def test_save_whatsapp_meta_stores_backend_and_creds(client, django_user_model):
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:pingboard_channel_save"), {
        "kind": "whatsapp", "label": "WA", "secret": "META-ACCESS-TOKEN",
        "routing_backend": "meta", "routing_meta_phone_id": "PH123",
        "routing_meta_api_version": "v21.0", "routing_to": "+15551234567",
        "routing_twilio_sid": "AC-should-be-dropped",  # not a meta field for this row
    })
    prov = ChannelProvider.objects.get(kind="whatsapp")
    assert prov.secret == "META-ACCESS-TOKEN"
    assert prov.routing == {
        "backend": "meta", "meta_phone_id": "PH123",
        "meta_api_version": "v21.0", "to": "+15551234567",
        "twilio_sid": "AC-should-be-dropped",
    }
    # the channel is operational with no env config at all
    from apps.admin_audit.console_pingboard import _provider_ready
    ok, _why = _provider_ready(prov)
    assert ok is True


@pytest.mark.django_db
def test_save_whatsapp_twilio_stores_creds(client, django_user_model):
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:pingboard_channel_save"), {
        "kind": "whatsapp", "label": "WA-T", "secret": "TWILIO-AUTH-TOKEN",
        "routing_backend": "twilio", "routing_twilio_sid": "AC777",
        "routing_twilio_from": "+14155238886", "routing_to": "+15551234567",
    })
    prov = ChannelProvider.objects.get(kind="whatsapp")
    assert prov.secret == "TWILIO-AUTH-TOKEN"
    assert prov.routing["backend"] == "twilio" and prov.routing["twilio_sid"] == "AC777"
    from apps.admin_audit.console_pingboard import _provider_ready
    assert _provider_ready(prov)[0] is True


# --- edit --------------------------------------------------------------------
@pytest.mark.django_db
def test_edit_form_prefills_and_hides_secret(client, django_user_model):
    client.force_login(_director(django_user_model))
    prov = ChannelProvider(kind="discord", label="Old")
    prov.secret = _WEBHOOK
    prov.save()
    html = client.get(reverse("admin_audit:pingboard_channel_edit", args=[prov.id])).content.decode()
    assert "Old" in html
    assert "DIF-SECRET-TOKEN-XYZ" not in html  # secret never rendered back
    assert "stored" in html.lower()  # tells the operator a secret is set


@pytest.mark.django_db
def test_update_changes_label_and_keeps_secret_when_blank(client, django_user_model):
    client.force_login(_director(django_user_model))
    prov = ChannelProvider(kind="discord", label="Old")
    prov.secret = _WEBHOOK
    prov.save()
    resp = client.post(reverse("admin_audit:pingboard_channel_update", args=[prov.id]),
                       {"kind": "discord", "label": "New name", "enabled": "on"})
    assert resp.status_code == 302
    prov.refresh_from_db()
    assert prov.label == "New name" and prov.enabled
    assert prov.secret == _WEBHOOK  # blank secret field ⇒ existing secret preserved


@pytest.mark.django_db
def test_update_can_clear_secret(client, django_user_model):
    client.force_login(_director(django_user_model))
    prov = ChannelProvider(kind="discord", label="D")
    prov.secret = _WEBHOOK
    prov.save()
    client.post(reverse("admin_audit:pingboard_channel_update", args=[prov.id]),
                {"kind": "discord", "label": "D", "clear_secret": "on"})
    prov.refresh_from_db()
    assert prov.has_secret is False


@pytest.mark.django_db
def test_update_replaces_secret(client, django_user_model):
    client.force_login(_director(django_user_model))
    prov = ChannelProvider(kind="discord", label="D")
    prov.secret = _WEBHOOK
    prov.save()
    new = "https://discord.com/api/webhooks/99/NEW-TOKEN"
    client.post(reverse("admin_audit:pingboard_channel_update", args=[prov.id]),
                {"kind": "discord", "label": "D", "secret": new})
    prov.refresh_from_db()
    assert prov.secret == new


# --- list --------------------------------------------------------------------
@pytest.mark.django_db
def test_list_shows_configure_link_and_target(client, django_user_model):
    client.force_login(_director(django_user_model))
    prov = ChannelProvider.objects.create(kind="telegram", label="TG",
                                          routing={"chat_id": "-100999"})
    html = client.get(reverse("admin_audit:pingboard_channels")).content.decode()
    assert reverse("admin_audit:pingboard_channel_edit", args=[prov.id]) in html
    assert "-100999" in html  # routing target summarised in the list
