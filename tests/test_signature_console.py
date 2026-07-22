"""Combat Signatures — WS-7 admin console tests.

Covers the leadership surface at ``/ops/admin/signatures/``: the per-screen role matrix (anonymous /
member / officer / director), the settings ModelForm (singleton write + nav-cache bust + audit),
background curation (toggle/reorder persist + audit + the disabled background leaving the WS-6
builder gallery), per-pilot search, the system-level moderation actions (admin disable/enable +
force re-render, on ANOTHER pilot's signature, with the public URL falling to its unavailable
state), the maintenance registry entries + their helpers, the health dashboard (parked-failure
render_error visible to a director, sane storage numbers) and the provenance mismatch warning.

House style: hermetic against the settings module — MEDIA_ROOT is per-test, the mirror is disabled,
the cache is cleared, and ``signature_render_task.delay`` is patched to record calls without Celery.
"""
from __future__ import annotations

import os

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.admin_audit import console as admin_console
from apps.admin_audit.models import AuditLog
from apps.identity.models import RoleAssignment
from apps.killboard import console_signatures, signature_pipeline, signatures, tasks
from apps.killboard.models import CombatSignature, CombatSignatureSettings, SignatureBackground
from apps.sso.services import ensure_role
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

ACTIVE = CombatSignature.Status.ACTIVE
DISABLED = CombatSignature.Status.DISABLED
FAILED = CombatSignature.RenderStatus.FAILED
PENDING = CombatSignature.RenderStatus.PENDING
NAV_CACHE_KEY = "kb:sig:nav_enabled"

# name -> (url name, minimum role that may open it)
SCREENS = {
    "dashboard": ("admin_audit:signatures_dashboard", rbac.ROLE_OFFICER),
    "search": ("admin_audit:signature_search", rbac.ROLE_OFFICER),
    "settings": ("admin_audit:signature_settings", rbac.ROLE_DIRECTOR),
    "backgrounds": ("admin_audit:signature_backgrounds", rbac.ROLE_DIRECTOR),
}


# --------------------------------------------------------------------------- #
#  Environment + builders
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def render_calls(settings, tmp_path, monkeypatch):
    """Isolate MEDIA_ROOT, disable the mirror, clear the cache and record enqueued renders."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.EVE_IMAGE_MIRROR_DIR = ""
    cache.clear()
    calls: list[int] = []
    monkeypatch.setattr(
        tasks.signature_render_task, "delay",
        lambda signature_id, *a, **k: calls.append(signature_id),
    )
    return calls


def _user(django_user_model, name, role):
    """A console user with the given account-level role and no pilot (mirrors the industry-console
    role tests): with no resolved pilot the LP-4 ceiling does not clamp, so the account grant
    stands and the gate is exercised cleanly."""
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _background(key="nebula-01", *, name="Nebula", enabled=True, order=0) -> SignatureBackground:
    bg, _created = SignatureBackground.objects.get_or_create(
        key=key, defaults={"name": name, "enabled": enabled, "display_order": order},
    )
    return bg


def _make_sig(char, bg, *, status=ACTIVE, name="Sig") -> CombatSignature:
    return CombatSignature.objects.create(
        character=char, name=name, background=bg, layout="identity", size_preset="standard",
        mode=CombatSignature.Mode.LIVE, status=status,
        config={"components": ["pilot_name", "kills"], "period": "30d",
                "featured_trophy_ids": [], "show_timestamp": False, "theme": "gold"},
    )


def _settings_post(**over) -> dict:
    data = {
        "max_active_per_pilot": "5",
        "refresh_interval_hours": "6",
        "max_featured_trophies": "4",
        "default_layout": "identity",
        "default_period": "30d",
        "allowed_size_presets": ["compact", "standard", "wide", "card"],
    }
    data.update(over)
    return data


# --------------------------------------------------------------------------- #
#  Role matrix (per screen)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("screen", list(SCREENS))
def test_anonymous_is_redirected_to_login(client, screen):
    resp = client.get(reverse(SCREENS[screen][0]))
    assert resp.status_code == 302
    assert "/login" in resp.url or "next=" in resp.url


@pytest.mark.parametrize("screen", list(SCREENS))
def test_member_is_forbidden(client, django_user_model, screen):
    client.force_login(_user(django_user_model, f"m-{screen}", rbac.ROLE_MEMBER))
    assert client.get(reverse(SCREENS[screen][0])).status_code == 403


@pytest.mark.parametrize("screen,expected", [
    ("dashboard", 200), ("search", 200), ("settings", 403), ("backgrounds", 403),
])
def test_officer_reaches_read_screens_only(client, django_user_model, screen, expected):
    client.force_login(_user(django_user_model, f"off-{screen}", rbac.ROLE_OFFICER))
    assert client.get(reverse(SCREENS[screen][0])).status_code == expected


@pytest.mark.parametrize("screen", list(SCREENS))
def test_director_reaches_every_screen(client, django_user_model, screen):
    client.force_login(_user(django_user_model, f"dir-{screen}", rbac.ROLE_DIRECTOR))
    assert client.get(reverse(SCREENS[screen][0])).status_code == 200


def test_hub_card_links_the_dashboard(client, django_user_model):
    client.force_login(_user(django_user_model, "off-hub", rbac.ROLE_OFFICER))
    html = client.get("/ops/admin/").content
    assert b"/ops/admin/signatures/" in html


# --------------------------------------------------------------------------- #
#  Settings (Director)
# --------------------------------------------------------------------------- #
def test_settings_save_updates_singleton_busts_nav_cache_and_audits(client, django_user_model):
    cache.set(NAV_CACHE_KEY, True, 600)
    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))

    resp = client.post(reverse("admin_audit:signature_settings"),
                       _settings_post(enabled="on", max_active_per_pilot="3"))
    assert resp.status_code == 302
    cfg = CombatSignatureSettings.load()
    assert cfg.enabled is True
    assert cfg.max_active_per_pilot == 3
    assert cache.get(NAV_CACHE_KEY) is None                     # nav master-switch cache busted

    log = AuditLog.objects.get(action="signatures.settings_update")
    assert "enabled" in log.metadata["changed"]
    assert log.metadata["enabled"] == {"old": False, "new": True}


def test_settings_rejects_a_zero_quota(client, django_user_model):
    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))
    resp = client.post(reverse("admin_audit:signature_settings"),
                       _settings_post(max_active_per_pilot="0"))
    assert resp.status_code == 200                              # re-rendered with the error
    assert CombatSignatureSettings.load().max_active_per_pilot != 0


# --------------------------------------------------------------------------- #
#  Background curation (Director)
# --------------------------------------------------------------------------- #
def test_backgrounds_toggle_and_reorder_persist_and_audit(client, django_user_model):
    keep = _background("keep-bg", name="KeepBg", order=0)
    drop = _background("drop-bg", name="DropBg", order=0)
    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))

    resp = client.post(reverse("admin_audit:signature_backgrounds"), {
        f"enabled_{keep.pk}": "1", f"order_{keep.pk}": "5",   # keep enabled, reordered
        f"order_{drop.pk}": "2",                              # drop: checkbox omitted -> disabled
    })
    assert resp.status_code == 302
    keep.refresh_from_db()
    drop.refresh_from_db()
    assert keep.display_order == 5 and keep.enabled is True
    assert drop.enabled is False and drop.display_order == 2
    assert AuditLog.objects.filter(action="signatures.background_toggle").exists()
    assert AuditLog.objects.filter(action="signatures.background_reorder").exists()


def test_disabled_background_leaves_the_builder_gallery(client, django_user_model):
    keep = _background("keep-bg", name="KeepBg")
    drop = _background("drop-bg", name="DropBg")
    cfg = CombatSignatureSettings.load()
    cfg.enabled = True
    cfg.save()
    member, _char = enrol_pilot(django_user_model, 7001)

    # Director disables the "drop" background.
    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:signature_backgrounds"), {
        f"enabled_{keep.pk}": "1", f"order_{keep.pk}": "0", f"order_{drop.pk}": "0",
    })
    drop.refresh_from_db()
    assert drop.enabled is False

    # The member's builder gallery now offers "keep" but not "drop".
    client.force_login(member)
    resp = client.get("/killboard/signatures/new/")
    assert resp.status_code == 200
    assert b"KeepBg" in resp.content
    assert b"DropBg" not in resp.content


# --------------------------------------------------------------------------- #
#  Search (Officer)
# --------------------------------------------------------------------------- #
def test_search_finds_by_name_and_by_character_id(client, django_user_model):
    _owner, char = enrol_pilot(django_user_model, 7100, name="Ace Pilot")
    _make_sig(char, _background(), name="MySig")
    client.force_login(_user(django_user_model, "off", rbac.ROLE_OFFICER))
    url = reverse("admin_audit:signature_search")

    assert b"MySig" in client.get(url, {"q": "Ace"}).content           # name substring
    assert b"MySig" in client.get(url, {"q": "7100"}).content          # exact character id
    assert b"MySig" not in client.get(url, {"q": "Zzzznomatch"}).content


# --------------------------------------------------------------------------- #
#  Moderation actions (Officer, system-level — no ownership)
# --------------------------------------------------------------------------- #
def test_admin_disable_moderates_another_pilots_signature(client, django_user_model):
    _owner, char = enrol_pilot(django_user_model, 7200)
    sig = _make_sig(char, _background())
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n")
    assert client.get(f"/s/{sig.public_token}.png").status_code == 200   # served before

    client.force_login(_user(django_user_model, "off", rbac.ROLE_OFFICER))
    resp = client.post(
        reverse("admin_audit:signature_admin_action", args=[sig.pk, "admin_disable"]), {"q": ""}
    )
    assert resp.status_code == 302
    sig.refresh_from_db()
    assert sig.status == DISABLED
    assert not os.path.exists(path)                                       # artifact removed
    assert AuditLog.objects.filter(
        action="signatures.admin_disable", target_id=str(sig.pk)).exists()
    # The public URL now falls to its constant-shape unavailable (404) state.
    assert client.get(f"/s/{sig.public_token}.png").status_code == 404


def test_admin_enable_and_regenerate_enqueue_and_audit(client, django_user_model, render_calls):
    _owner, char = enrol_pilot(django_user_model, 7300)
    sig = _make_sig(char, _background(), status=DISABLED)
    client.force_login(_user(django_user_model, "off", rbac.ROLE_OFFICER))

    client.post(reverse("admin_audit:signature_admin_action", args=[sig.pk, "admin_enable"]), {"q": ""})
    sig.refresh_from_db()
    assert sig.status == ACTIVE
    assert sig.pk in render_calls
    assert AuditLog.objects.filter(action="signatures.admin_enable", target_id=str(sig.pk)).exists()

    render_calls.clear()
    client.post(reverse("admin_audit:signature_admin_action", args=[sig.pk, "regenerate"]), {"q": ""})
    assert render_calls == [sig.pk]
    assert AuditLog.objects.filter(
        action="signatures.admin_regenerate", target_id=str(sig.pk)).exists()


def test_unknown_admin_action_404s(client, django_user_model):
    _owner, char = enrol_pilot(django_user_model, 7301)
    sig = _make_sig(char, _background())
    client.force_login(_user(django_user_model, "off", rbac.ROLE_OFFICER))
    assert client.post(
        reverse("admin_audit:signature_admin_action", args=[sig.pk, "frobnicate"])
    ).status_code == 404


# --------------------------------------------------------------------------- #
#  Maintenance registry + helpers
# --------------------------------------------------------------------------- #
def test_maintenance_registry_has_both_signature_entries():
    reg = admin_console._MAINTENANCE_TASKS
    assert reg["signatures_rerender_all"][0] == "killboard.signature_rerender_all"
    assert reg["signatures_cleanup_orphans"][0] == "killboard.signature_cleanup"


def test_rerender_all_marks_active_dirty_and_clears_ledger(django_user_model):
    _owner, char = enrol_pilot(django_user_model, 7400)
    bg = _background()
    active = _make_sig(char, bg, name="Active")
    active.dirty = False
    active.render_status = FAILED
    active.consecutive_failures = 5
    active.render_error = "boom"
    active.save()
    disabled = _make_sig(char, bg, name="Disabled", status=DISABLED)
    disabled.dirty = False
    disabled.consecutive_failures = 5
    disabled.save()

    n = signature_pipeline.rerender_all()
    assert n == 1                                                # only the ACTIVE one
    active.refresh_from_db()
    disabled.refresh_from_db()
    assert active.dirty is True and active.render_status == PENDING
    assert active.consecutive_failures == 0 and active.render_error == ""
    # A disabled signature is untouched (its banner stays down).
    assert disabled.dirty is False and disabled.consecutive_failures == 5


def test_rerender_all_task_runs_the_helper(django_user_model):
    _owner, char = enrol_pilot(django_user_model, 7401)
    _make_sig(char, _background())
    assert tasks.signature_rerender_all_task() == 1


def test_cleanup_orphans_task_is_callable(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    # Missing media dir → the janitor refuses (-1); once it exists, an empty sweep returns 0.
    assert tasks.signature_cleanup_task() == -1
    os.makedirs(os.path.join(str(tmp_path), "signatures"))
    assert tasks.signature_cleanup_task() == 0


# --------------------------------------------------------------------------- #
#  Dashboard (Officer) — health, storage, provenance
# --------------------------------------------------------------------------- #
def test_dashboard_shows_parked_failure_error_and_sane_storage(client, django_user_model):
    _owner, char = enrol_pilot(django_user_model, 7500)
    sig = _make_sig(char, _background())
    sig.render_status = FAILED
    sig.consecutive_failures = 5
    sig.render_error = "render blew up spectacularly"
    sig.save()
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n" * 200)

    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))
    resp = client.get(reverse("admin_audit:signatures_dashboard"))
    assert resp.status_code == 200
    # The render_error is admin-visible here (never shown to owners).
    assert b"render blew up spectacularly" in resp.content
    storage = resp.context["storage"]
    assert storage["count"] == 1 and storage["bytes"] > 0


def test_dashboard_flags_a_provenance_checksum_mismatch(client, django_user_model, monkeypatch):
    bg = _background("nebula-x", name="NebX")
    bg.checksum = "committed-checksum"
    bg.save()
    fake = {"backgrounds": [{"key": "nebula-x", "checksum": "DIFFERENT", "files": {}}]}
    monkeypatch.setattr(console_signatures.signature_assets, "load_manifest", lambda *a, **k: fake)

    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))
    resp = client.get(reverse("admin_audit:signatures_dashboard"))
    assert resp.status_code == 200
    prov = resp.context["provenance"]
    assert prov["ok"] is False
    assert any(m["key"] == "nebula-x" for m in prov["mismatches"])
    assert b"Attention needed" in resp.content


def test_dashboard_flags_a_manifest_load_failure(client, django_user_model, monkeypatch):
    _background("nebula-y")

    def _boom(*a, **k):
        raise OSError("no manifest here")

    monkeypatch.setattr(console_signatures.signature_assets, "load_manifest", _boom)
    client.force_login(_user(django_user_model, "dir", rbac.ROLE_DIRECTOR))
    resp = client.get(reverse("admin_audit:signatures_dashboard"))
    assert resp.status_code == 200
    assert resp.context["provenance"]["manifest_error"]
    assert resp.context["provenance"]["ok"] is False
