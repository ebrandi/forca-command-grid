"""Combat Signatures — WS-6 management UI view tests.

Exercises the private, owner-scoped editor at ``/killboard/signatures/``: the feature/membership
gate (anonymous / non-member / feature-disabled / enabled), IDOR (another account, and a
linked-but-not-acting pilot), the create + edit builders (quota, invalid config, XSS name, the
config_version bump + failure-ledger reset, the snapshot config-lock), every POST action
(regenerate / duplicate / snapshot / rotate / disable / enable / delete + audit + artifact
removal), the owner-only synchronous preview (PNG dims, throttle, unsaved), the nav flag, and the
embed snippets (exact public URL + escaped HTML alt).

House style: hermetic against the settings module (``.delay`` patched, MEDIA_ROOT per-test, mirror
disabled, cache cleared), image assertions check PNG structure + preset dims (never pixels).
"""
from __future__ import annotations

import io

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.admin_audit.models import AuditLog
from apps.killboard import signatures, tasks
from apps.killboard.models import CombatSignature, CombatSignatureSettings, SignatureBackground
from apps.killboard.signature_render import PRESETS
from apps.sso.models import EveCharacter
from tests._raffle_utils import add_token, enrol_pilot

pytestmark = pytest.mark.django_db

ACTIVE = CombatSignature.Status.ACTIVE
DISABLED = CombatSignature.Status.DISABLED
FROZEN = CombatSignature.Status.FROZEN
LIVE = CombatSignature.Mode.LIVE
SNAPSHOT = CombatSignature.Mode.SNAPSHOT

LIST_URL = "/killboard/signatures/"
NEW_URL = "/killboard/signatures/new/"
PREVIEW_URL = "/killboard/signatures/preview.png"


# --------------------------------------------------------------------------- #
#  Environment: per-test MEDIA_ROOT, no network, clean cache, patched .delay.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def render_calls(settings, tmp_path, monkeypatch):
    """Autouse env: isolates MEDIA_ROOT, disables the mirror, clears the (throttle/feature/nav)
    cache, and records ``signature_render_task.delay(pk)`` calls without enqueuing Celery."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.EVE_IMAGE_MIRROR_DIR = ""
    cache.clear()
    calls: list[int] = []
    monkeypatch.setattr(
        tasks.signature_render_task, "delay",
        lambda signature_id, *a, **k: calls.append(signature_id),
    )
    return calls


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _enable(**over) -> CombatSignatureSettings:
    cfg = CombatSignatureSettings.load()
    cfg.enabled = True
    for key, value in over.items():
        setattr(cfg, key, value)
    cfg.save()
    return cfg


def _background(key="nebula-01") -> SignatureBackground:
    bg, _created = SignatureBackground.objects.get_or_create(
        key=key, defaults={"name": "Nebula", "enabled": True}
    )
    return bg


def _make_sig(char, bg, *, mode=LIVE, status=ACTIVE, components=("pilot_name", "kills"),
              config_version=1, name="Sig") -> CombatSignature:
    snap = timezone.now() if mode == SNAPSHOT else None
    return CombatSignature.objects.create(
        character=char, name=name, background=bg, layout="identity", size_preset="standard",
        mode=mode, status=status, config_version=config_version, snapshot_taken_at=snap,
        config={"components": list(components), "period": "30d", "featured_trophy_ids": [],
                "show_timestamp": False, "theme": "gold"},
    )


def _form_data(bg, **over) -> dict:
    data = {
        "name": "My Sig", "layout": "identity", "size_preset": "standard",
        "background": str(bg.id), "language": "", "period": "30d", "theme": "gold",
        "components": ["pilot_name", "kills"],
    }
    data.update(over)
    return data


def _add_pilot(user, character_id, *, name="Alt", is_corp_member=True) -> EveCharacter:
    char = EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=False,
        is_corp_member=is_corp_member,
    )
    add_token(char)
    return char


# --------------------------------------------------------------------------- #
#  Permission matrix (A17)
# --------------------------------------------------------------------------- #
def test_anonymous_is_redirected_to_login(client):
    _enable()
    resp = client.get(LIST_URL)
    assert resp.status_code == 302
    assert "/login" in resp.url or "next=" in resp.url


def test_authenticated_non_member_is_gated(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 6001, roles=(), is_corp_member=False)
    client.force_login(user)
    resp = client.get(LIST_URL)
    # MembershipGateMiddleware confines a logged-in non-member to the recruitment surface.
    assert resp.status_code == 302
    assert "onboarding" in resp.url


def test_member_with_feature_disabled_gets_404(client, django_user_model):
    # Settings default to disabled; the killboard feature stays enabled.
    user, _char = enrol_pilot(django_user_model, 6002)
    client.force_login(user)
    assert client.get(LIST_URL).status_code == 404


def test_member_with_feature_enabled_gets_200(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 6003)
    client.force_login(user)
    assert client.get(LIST_URL).status_code == 200


# --------------------------------------------------------------------------- #
#  IDOR — ownership is the query
# --------------------------------------------------------------------------- #
def test_other_account_cannot_touch_a_signature(client, django_user_model):
    _enable()
    _owner, owner_char = enrol_pilot(django_user_model, 6100)
    sig = _make_sig(owner_char, _background())
    attacker, _c = enrol_pilot(django_user_model, 6101)
    client.force_login(attacker)

    assert client.get(f"/killboard/signatures/{sig.pk}/edit/").status_code == 404
    for action in ("regenerate", "rotate", "disable", "delete"):
        resp = client.post(f"/killboard/signatures/{sig.pk}/{action}/")
        assert resp.status_code == 404, action
    # Untouched.
    assert CombatSignature.objects.filter(pk=sig.pk).exists()


def test_linked_but_not_acting_pilot_is_rejected(client, django_user_model):
    _enable()
    user, main = enrol_pilot(django_user_model, 6200)
    alt = _add_pilot(user, 6201)
    sig = _make_sig(main, _background())
    client.force_login(user)
    # Fly the alt: the main's signature must no longer resolve for this session.
    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})

    assert client.get(f"/killboard/signatures/{sig.pk}/edit/").status_code == 404
    assert client.post(f"/killboard/signatures/{sig.pk}/delete/").status_code == 404
    assert CombatSignature.objects.filter(pk=sig.pk).exists()


# --------------------------------------------------------------------------- #
#  Create
# --------------------------------------------------------------------------- #
def test_create_happy_path_enqueues_first_render(client, django_user_model, render_calls):
    _enable()
    user, char = enrol_pilot(django_user_model, 6300)
    bg = _background()
    client.force_login(user)

    resp = client.post(NEW_URL, _form_data(bg, name="Fresh"))
    assert resp.status_code == 302 and resp.url == LIST_URL
    sig = CombatSignature.objects.get(character_id=char.character_id)
    assert sig.name == "Fresh"
    assert render_calls == [sig.pk]              # first render queued immediately


def test_create_at_quota_is_a_form_error(client, django_user_model):
    _enable(max_active_per_pilot=1)
    user, char = enrol_pilot(django_user_model, 6301)
    bg = _background()
    _make_sig(char, bg)                          # already at the cap of 1
    client.force_login(user)

    resp = client.post(NEW_URL, _form_data(bg))
    assert resp.status_code == 400
    assert CombatSignature.objects.filter(character_id=char.character_id).count() == 1


def test_create_invalid_config_is_a_form_error(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6302)
    bg = _background()
    client.force_login(user)

    resp = client.post(NEW_URL, _form_data(bg, layout="bogus"))
    assert resp.status_code == 400
    assert b"Invalid layout" in resp.content
    assert not CombatSignature.objects.filter(character_id=char.character_id).exists()


def test_create_xss_name_is_escaped_in_the_list(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 6303)
    bg = _background()
    client.force_login(user)

    payload = "<script>alert(1)</script>"
    client.post(NEW_URL, _form_data(bg, name=payload))
    resp = client.get(LIST_URL)
    assert resp.status_code == 200
    assert b"<script>alert(1)</script>" not in resp.content
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in resp.content


def test_create_form_renders(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 6310)
    bg = _background()
    client.force_login(user)

    resp = client.get(NEW_URL)
    assert resp.status_code == 200
    assert b'name="name"' in resp.content
    assert b'name="components"' in resp.content       # the ordering widget seed markup
    assert b'json_script' not in resp.content         # the filter ran (didn't leak literally)
    assert bg.name.encode() in resp.content           # background gallery rendered


# --------------------------------------------------------------------------- #
#  Edit
# --------------------------------------------------------------------------- #
def test_edit_form_renders_for_live(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6410)
    sig = _make_sig(char, _background(), name="Editable")
    client.force_login(user)

    resp = client.get(f"/killboard/signatures/{sig.pk}/edit/")
    assert resp.status_code == 200
    assert b"Editable" in resp.content
    assert b'formaction="/killboard/signatures/preview.png"' in resp.content


def test_edit_form_locks_config_for_snapshot(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6411)
    sig = _make_sig(char, _background(), mode=SNAPSHOT, name="Frozen")
    client.force_login(user)

    resp = client.get(f"/killboard/signatures/{sig.pk}/edit/")
    assert resp.status_code == 200
    assert b'name="name"' in resp.content              # rename still offered
    # The config controls + preview are withheld on a frozen snapshot.
    assert b'name="components"' not in resp.content
    assert b"preview.png" not in resp.content



def test_edit_config_change_bumps_version_and_resets_ledger(client, django_user_model, render_calls):
    _enable()
    user, char = enrol_pilot(django_user_model, 6400)
    bg = _background()
    sig = _make_sig(char, bg)
    # A parked, previously-failed render must retry under the new config.
    sig.render_status = CombatSignature.RenderStatus.FAILED
    sig.consecutive_failures = 3
    sig.render_error = "boom"
    sig.dirty = False
    sig.save()
    client.force_login(user)

    resp = client.post(f"/killboard/signatures/{sig.pk}/edit/",
                       _form_data(bg, components=["pilot_name", "kills", "losses"]))
    assert resp.status_code == 302
    sig.refresh_from_db()
    assert sig.config_version == 2
    assert sig.dirty is True
    assert sig.render_status == CombatSignature.RenderStatus.PENDING
    assert sig.consecutive_failures == 0 and sig.render_error == ""
    assert sig.config["components"] == ["pilot_name", "kills", "losses"]
    assert render_calls == [sig.pk]


def test_edit_snapshot_renames_but_rejects_config_change(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6401)
    bg = _background()
    sig = _make_sig(char, bg, mode=SNAPSHOT, name="Frozen")
    original_config = dict(sig.config)
    client.force_login(user)

    resp = client.post(f"/killboard/signatures/{sig.pk}/edit/",
                       _form_data(bg, name="Renamed", components=["pilot_name", "kills", "losses"]))
    assert resp.status_code == 302
    sig.refresh_from_db()
    assert sig.name == "Renamed"                 # rename applied
    assert sig.config == original_config         # config frozen
    assert sig.config_version == 1
    assert sig.mode == SNAPSHOT


# --------------------------------------------------------------------------- #
#  Actions
# --------------------------------------------------------------------------- #
def test_regenerate_enqueues_and_audits(client, django_user_model, render_calls):
    _enable()
    user, char = enrol_pilot(django_user_model, 6500)
    sig = _make_sig(char, _background())
    client.force_login(user)

    resp = client.post(f"/killboard/signatures/{sig.pk}/regenerate/")
    assert resp.status_code == 302
    assert render_calls == [sig.pk]
    assert AuditLog.objects.filter(action="signatures.regenerate", target_id=str(sig.pk)).exists()


def test_duplicate_respects_quota(client, django_user_model):
    _enable(max_active_per_pilot=1)
    user, char = enrol_pilot(django_user_model, 6501)
    sig = _make_sig(char, _background())
    client.force_login(user)

    resp = client.post(f"/killboard/signatures/{sig.pk}/duplicate/")
    assert resp.status_code == 302                # redirects with an error message
    assert CombatSignature.objects.filter(character_id=char.character_id).count() == 1


def test_duplicate_creates_a_copy_under_quota(client, django_user_model):
    _enable(max_active_per_pilot=5)
    user, char = enrol_pilot(django_user_model, 6502)
    sig = _make_sig(char, _background())
    client.force_login(user)

    client.post(f"/killboard/signatures/{sig.pk}/duplicate/")
    assert CombatSignature.objects.filter(character_id=char.character_id).count() == 2


def test_delete_removes_row_and_artifact(client, django_user_model):
    import os
    _enable()
    user, char = enrol_pilot(django_user_model, 6503)
    sig = _make_sig(char, _background())
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n")
    client.force_login(user)

    resp = client.post(f"/killboard/signatures/{sig.pk}/delete/")
    assert resp.status_code == 302
    assert not CombatSignature.objects.filter(pk=sig.pk).exists()
    assert not os.path.exists(path)
    assert AuditLog.objects.filter(action="signatures.delete", target_id=str(sig.pk)).exists()


def test_rotate_changes_token_and_old_url_404s(client, django_user_model):
    import os
    _enable()
    user, char = enrol_pilot(django_user_model, 6504)
    sig = _make_sig(char, _background())
    old_token = sig.public_token
    # An artifact at the OLD token, so the old public URL would 200 until rotation drops it.
    path = signatures.artifact_path(old_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n")
    client.force_login(user)

    client.post(f"/killboard/signatures/{sig.pk}/rotate/")
    sig.refresh_from_db()
    assert sig.public_token != old_token
    assert client.get(f"/s/{old_token}.png").status_code == 404


def test_disable_then_enable(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6505)
    sig = _make_sig(char, _background())
    client.force_login(user)

    client.post(f"/killboard/signatures/{sig.pk}/disable/")
    sig.refresh_from_db()
    assert sig.status == DISABLED
    client.post(f"/killboard/signatures/{sig.pk}/enable/")
    sig.refresh_from_db()
    assert sig.status == ACTIVE


def test_snapshot_action_converts_live_signature(client, django_user_model):
    _enable(snapshots_enabled=True)
    user, char = enrol_pilot(django_user_model, 6506)
    sig = _make_sig(char, _background())
    client.force_login(user)

    client.post(f"/killboard/signatures/{sig.pk}/snapshot/")
    sig.refresh_from_db()
    assert sig.mode == SNAPSHOT
    assert sig.snapshot_taken_at is not None


def test_unknown_action_404s(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6507)
    sig = _make_sig(char, _background())
    client.force_login(user)
    assert client.post(f"/killboard/signatures/{sig.pk}/frobnicate/").status_code == 404


# --------------------------------------------------------------------------- #
#  Preview
# --------------------------------------------------------------------------- #
def test_preview_returns_png_of_the_right_size_without_saving(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 6600)
    bg = _background()
    client.force_login(user)

    before = CombatSignature.objects.count()
    resp = client.post(PREVIEW_URL, _form_data(bg, size_preset="wide"))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    assert resp["Cache-Control"] == "no-store"
    img = Image.open(io.BytesIO(resp.content))
    assert img.format == "PNG" and img.size == PRESETS["wide"]
    assert CombatSignature.objects.count() == before          # nothing persisted


def test_preview_fetches_portrait_with_a_bounded_timeout(client, django_user_model, monkeypatch):
    # The preview must reflect the published image (real portrait), but fetch it with the short
    # interactive budget so a cold fetch can't hold a web worker — not skip the fetch entirely.
    from apps.killboard import signature_stats
    from apps.killboard.signature_views import _PREVIEW_FETCH_TIMEOUT

    seen = {}

    def _spy(character_id, *a, **kw):
        seen["timeout"] = kw.get("timeout")
        return None                                           # degrade to monogram (no network)

    monkeypatch.setattr(signature_stats.assets, "ensure_portrait", _spy)
    _enable()
    user, _char = enrol_pilot(django_user_model, 6602)
    bg = _background()
    client.force_login(user)

    resp = client.post(PREVIEW_URL, _form_data(bg, components=["portrait", "pilot_name", "kills"]))
    assert resp.status_code == 200
    assert seen["timeout"] == _PREVIEW_FETCH_TIMEOUT          # fetched, and bounded


def test_preview_is_throttled(client, django_user_model, settings):
    settings.SIGNATURE_PREVIEW_RATE = 1
    _enable()
    user, _char = enrol_pilot(django_user_model, 6601)
    bg = _background()
    client.force_login(user)

    assert client.post(PREVIEW_URL, _form_data(bg)).status_code == 200
    resp = client.post(PREVIEW_URL, _form_data(bg))
    assert resp.status_code == 429
    assert resp["Retry-After"] == "60"


def test_preview_requires_login(client):
    _enable()
    resp = client.post(PREVIEW_URL, {"name": "x"})
    assert resp.status_code == 302                            # gated to login


# --------------------------------------------------------------------------- #
#  Nav flag
# --------------------------------------------------------------------------- #
def test_nav_link_present_when_enabled(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 6700)
    client.force_login(user)
    resp = client.get(reverse("killboard:list"))
    assert b'href="/killboard/signatures/"' in resp.content


def test_nav_link_absent_when_disabled(client, django_user_model):
    # Settings default to disabled.
    user, _char = enrol_pilot(django_user_model, 6701)
    client.force_login(user)
    resp = client.get(reverse("killboard:list"))
    assert b'href="/killboard/signatures/"' not in resp.content


# --------------------------------------------------------------------------- #
#  Embed snippets
# --------------------------------------------------------------------------- #
def test_embed_snippets_carry_the_exact_public_url(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 6800)
    sig = _make_sig(char, _background())
    client.force_login(user)

    resp = client.get(LIST_URL)
    url = f"http://testserver/s/{sig.public_token}.png"
    assert f"[img]{url}[/img]".encode() in resp.content
    assert url.encode() in resp.content                       # direct + markdown + html all carry it


def test_embed_html_alt_is_escaped():
    snippets = signatures.embed_snippets(
        "http://testserver/s/abcdefghijklmnopqrstuv.png",
        'Ace "Killer" <b> — Combat Signature',
    )
    # The HTML variant must not let a quote/angle-bracket break out of the alt="" attribute.
    assert '&quot;' in snippets["html"] and '&lt;b&gt;' in snippets["html"]
    assert '"Killer"' not in snippets["html"]
    assert snippets["bbcode"] == "[img]http://testserver/s/abcdefghijklmnopqrstuv.png[/img]"


def test_alt_text_is_capped_to_80_chars():
    sig = CombatSignature(name="X" * 60)
    alt = signatures.signature_alt_text(sig)
    assert len(alt) <= 80
