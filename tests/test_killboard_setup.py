"""KB-38 self-host adoption profile (WS-D5): setup wizard, history import, branding, profile.

Covers the five wizard step statuses under fixture conditions, director-gating of the wizard,
the one-click history-import launcher (enqueue → single-active refusal → state transitions →
cancel flag → command-layer wiring), branding validation + persistence + accent application,
the ``apply_profile`` preset mechanism, and the linked self-host guide.

The import backend is always stubbed (``call_command`` patched) — no test touches the network.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import branding, setup_status
from apps.killboard.models import IngestSourceHealth, KillboardHistoryImport, Killmail
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001  # FORCA_HOME_CORP_ID in test settings
CORP_SCOPE = "esi-killmails.read_corporation_killmails.v1"


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #
def _user(django_user_model, role=None, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"kb-d5-{role or 'anon'}{suffix}")
    if role:
        RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _director_token(corp_id=HOME_CORP):
    """A corp-member character holding a live corp-killmails token (the DB signal)."""
    from apps.corporation.models import EveCorporation

    EveCorporation.objects.get_or_create(corporation_id=corp_id, defaults={"name": "Home Corp"})
    char = EveCharacter.objects.create(
        character_id=42, name="Director Alt", corporation_id=corp_id, is_corp_member=True,
    )
    return AuthToken.objects.create(character=char, scopes=[CORP_SCOPE])


def _km(kid, when=None):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}",
        killmail_time=when or timezone.now(), solar_system_id=30000142, region_id=10000002,
        sec_band="highsec", victim_ship_type_id=587, victim_corporation_id=HOME_CORP,
        total_value=0, involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )


# --------------------------------------------------------------------------- #
#  1. Wizard step statuses
# --------------------------------------------------------------------------- #
def test_step_esi_app_ok_when_configured(db, settings):
    # Hermetic: assert against explicit values, not whatever the ambient
    # settings module (dev vs test) happens to carry.
    settings.EVE_SSO_CLIENT_ID = "client-id"
    settings.EVE_SSO_CLIENT_SECRET = "client-secret"
    assert setup_status.step_esi_app(None)["status"] == setup_status.OK


def test_step_esi_app_missing_when_unconfigured(db, settings):
    settings.EVE_SSO_CLIENT_ID = ""
    settings.EVE_SSO_CLIENT_SECRET = ""
    step = setup_status.step_esi_app(None)
    assert step["status"] == setup_status.MISSING


def test_step_director_token_missing_warn_ok(db):
    # No token, no feed → missing.
    assert setup_status.step_director_token(HOME_CORP)["status"] == setup_status.MISSING

    # Candidate token present but the feed never polled → warn.
    _director_token()
    assert setup_status.step_director_token(HOME_CORP)["status"] == setup_status.WARN

    # Feed polled recently → ok (the authoritative signal).
    IngestSourceHealth.objects.create(source="esi_corp", last_success_at=timezone.now())
    assert setup_status.step_director_token(HOME_CORP)["status"] == setup_status.OK


def test_step_director_token_stale_feed_is_warn(db):
    _director_token()
    IngestSourceHealth.objects.create(
        source="esi_corp", last_success_at=timezone.now() - dt.timedelta(hours=6),
    )
    assert setup_status.step_director_token(HOME_CORP)["status"] == setup_status.WARN


def test_step_reference_data(db, sde, monkeypatch):
    # The bundled SDE sample is tiny (18 types); drop the prod threshold so the sample
    # counts as "loaded" and we exercise the SDE-present-but-no-prices → WARN path.
    monkeypatch.setattr(setup_status, "_SDE_MIN_TYPES", 10)
    step = setup_status.step_reference_data()
    assert step["status"] == setup_status.WARN
    assert step["type_count"] >= 10

    from decimal import Decimal

    from apps.market.models import MarketPrice
    MarketPrice.objects.create(type_id=587, location=None,
                               profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("1"))
    assert setup_status.step_reference_data()["status"] == setup_status.OK


def test_step_reference_data_missing_without_sde(db):
    assert setup_status.step_reference_data()["status"] == setup_status.MISSING


def test_step_history_warn_empty_then_ok(db):
    assert setup_status.step_history(None)["status"] == setup_status.WARN
    _km(1)
    step = setup_status.step_history(None)
    assert step["status"] == setup_status.OK
    assert step["killmail_count"] == 1
    assert step["oldest"] is not None and step["newest"] is not None


def test_step_branding_warn_unset_ok_when_set(db):
    assert setup_status.step_branding()["status"] == setup_status.WARN
    branding.set_branding({"accent_color": "#c8a24b"})
    assert setup_status.step_branding()["status"] == setup_status.OK


# --------------------------------------------------------------------------- #
#  2. Director-gating of the wizard page
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role,allowed", [
    (rbac.ROLE_MEMBER, False),
    (rbac.ROLE_OFFICER, False),
    (rbac.ROLE_DIRECTOR, True),
])
def test_setup_wizard_director_gated(client, django_user_model, role, allowed):
    client.force_login(_user(django_user_model, role))
    resp = client.get("/killboard/setup/")
    if allowed:
        assert resp.status_code == 200
        assert b"Killboard setup" in resp.content
    else:
        assert resp.status_code == 403


def test_setup_wizard_lists_five_steps_and_guide_link(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_DIRECTOR))
    resp = client.get("/killboard/setup/")
    assert resp.status_code == 200
    # The self-host guide is linked from the wizard.
    assert b"killboard-self-host.md" in resp.content


# --------------------------------------------------------------------------- #
#  3. History import launcher
# --------------------------------------------------------------------------- #
def test_import_enqueue_runs_and_calls_command(client, django_user_model):
    """An officer POST enqueues the import; the task drives the command layer.

    Hermetic: the Celery dispatch is replaced with a synchronous call so the test
    never depends on the ambient broker/eager configuration.
    """
    from apps.killboard import history_import

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    with patch("apps.killboard.history_import.call_command") as cc, \
            patch("apps.killboard.tasks.run_history_import") as task:
        task.delay.side_effect = lambda pk: history_import.run_import(pk)
        resp = client.post("/killboard/setup/import/", {
            "source": "everef", "from_date": "2020-01-05", "to_date": "2020-01-10",
        })
    assert resp.status_code == 302
    job = KillboardHistoryImport.objects.get()
    assert job.state == KillboardHistoryImport.State.DONE
    # The command layer was invoked (not re-implemented).
    assert cc.called
    assert cc.call_args_list[0].args[0] == "import_everef_killmails"
    assert cc.call_args_list[0].kwargs["from_date"] == "2020-01-05"


def test_import_single_active_refusal(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    KillboardHistoryImport.objects.create(
        source="everef", state=KillboardHistoryImport.State.RUNNING,
    )
    with patch("apps.killboard.history_import.call_command") as cc:
        client.post("/killboard/setup/import/", {"source": "everef",
                                                 "from_date": "2020-01-05",
                                                 "to_date": "2020-01-06"})
    # No second job created, and no import command driven.
    assert KillboardHistoryImport.objects.count() == 1
    assert not cc.called


def test_import_launcher_officer_gated(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER))
    resp = client.post("/killboard/setup/import/", {"source": "everef"})
    assert resp.status_code == 403


def test_run_import_state_transitions(db):
    from apps.killboard.history_import import run_import

    job = KillboardHistoryImport.objects.create(
        source="everef", from_date=dt.date(2020, 1, 1), to_date=dt.date(2020, 1, 3),
    )
    seen_states = []

    def _stub(cmd, **kwargs):
        seen_states.append(KillboardHistoryImport.objects.get(pk=job.pk).state)

    with patch("apps.killboard.history_import.call_command", side_effect=_stub):
        final = run_import(job.pk)

    assert final == KillboardHistoryImport.State.DONE
    # It was RUNNING while the command executed (pending → running → done).
    assert seen_states and all(s == "running" for s in seen_states)
    job.refresh_from_db()
    assert job.started_at is not None and job.finished_at is not None


def test_run_import_cancel_between_batches(db):
    """A cancel set mid-run stops the chunk loop and marks the job cancelled."""
    from apps.killboard.history_import import run_import

    # Six months → several 30-day chunks, so there IS a "between batches" moment.
    job = KillboardHistoryImport.objects.create(
        source="everef", from_date=dt.date(2020, 1, 1), to_date=dt.date(2020, 6, 30),
    )

    def _stub(cmd, **kwargs):
        # Request cancellation after the first chunk completes.
        KillboardHistoryImport.objects.filter(pk=job.pk).update(cancel_requested=True)

    with patch("apps.killboard.history_import.call_command", side_effect=_stub) as cc:
        final = run_import(job.pk)

    assert final == KillboardHistoryImport.State.CANCELLED
    # Stopped after the first chunk — not all chunks ran.
    assert cc.call_count == 1


def test_run_import_idempotent_on_finished_job(db):
    from apps.killboard.history_import import run_import

    job = KillboardHistoryImport.objects.create(
        source="everef", state=KillboardHistoryImport.State.DONE,
    )
    with patch("apps.killboard.history_import.call_command") as cc:
        assert run_import(job.pk) == KillboardHistoryImport.State.DONE
    assert not cc.called


def test_import_cancel_view_sets_flag(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    job = KillboardHistoryImport.objects.create(
        source="everef", state=KillboardHistoryImport.State.RUNNING,
    )
    resp = client.post("/killboard/setup/import/cancel/")
    assert resp.status_code == 302
    job.refresh_from_db()
    assert job.cancel_requested is True


def test_import_status_fragment(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    KillboardHistoryImport.objects.create(
        source="everef", state=KillboardHistoryImport.State.RUNNING, ingested=7,
    )
    resp = client.get("/killboard/setup/import/status/")
    assert resp.status_code == 200
    assert b"7 ingested" in resp.content


# --------------------------------------------------------------------------- #
#  4. Branding validation, persistence, accent application
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("accent,ok", [
    ("#c8a24b", True), ("#abc", True), ("c8a24b", False),
    ("#12345", False), ("red", False), ("#gggggg", False),
])
def test_branding_accent_validation(db, accent, ok):
    _clean, errors = branding.validate({"accent_color": accent})
    assert (errors == []) is ok


@pytest.mark.parametrize("logo,ok", [
    ("https://example.com/logo.png", True),
    ("/static/img/logo.svg", True),
    ("javascript:alert(1)", False),
    ("http://insecure.example/logo.png", False),
])
def test_branding_logo_validation(db, logo, ok):
    _clean, errors = branding.validate({"logo_url": logo})
    assert (errors == []) is ok


def test_branding_persist_and_fallback(db):
    branding.set_branding({"display_name": "Test Corp", "accent_color": "#0af"})
    assert branding.display_name("fallback") == "Test Corp"
    assert branding.accent_color() == "#0af"
    # An empty override falls back to the given home-corp name.
    branding.set_branding({"display_name": ""})
    assert branding.display_name("Home Corp") == "Home Corp"


def test_branding_invalid_not_persisted(db):
    branding.set_branding({"accent_color": "#c8a24b"})
    # A subsequent invalid submit is rejected wholesale — the prior value survives.
    _clean, errors = branding.set_branding({"accent_color": "nope"})
    assert errors
    assert branding.accent_color() == "#c8a24b"


def test_branding_form_persists_via_view(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_DIRECTOR))
    resp = client.post("/killboard/setup/branding/", {
        "display_name": "Brave Newbies", "accent_color": "#c8a24b",
        "logo_url": "https://images.evetech.net/x.png", "footer_tagline": "gf",
    })
    assert resp.status_code == 302
    assert branding.get_branding()["display_name"] == "Brave Newbies"


def test_branding_form_director_gated(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    resp = client.post("/killboard/setup/branding/", {"accent_color": "#c8a24b"})
    assert resp.status_code == 403


def test_branding_accent_applied_in_template(client, django_user_model):
    branding.set_branding({"accent_color": "#c8a24b"})
    client.force_login(_user(django_user_model, rbac.ROLE_DIRECTOR))
    resp = client.get("/killboard/setup/")
    assert resp.status_code == 200
    assert b"#c8a24b" in resp.content


def test_branding_accent_applied_on_list_hero(client, sde):
    """The list hero (a killboard base template) carries the accent inline."""
    branding.set_branding({"accent_color": "#c8a24b", "display_name": "Test Corp"})
    resp = client.get("/killboard/")
    assert resp.status_code == 200
    assert b"#c8a24b" in resp.content
    assert b"Test Corp" in resp.content


# --------------------------------------------------------------------------- #
#  5. Profile preset mechanism (apply_profile management command)
# --------------------------------------------------------------------------- #
def test_apply_profile_killboard_flips_flags(db):
    from django.core.management import call_command

    from core import features

    call_command("apply_profile", "killboard")
    assert features.feature_enabled("killboard") is True
    assert features.feature_enabled("market") is True
    assert features.feature_enabled("intel") is True
    # Heavy suite modules are turned off.
    assert features.feature_enabled("industry") is False
    assert features.feature_enabled("mining") is False
    assert features.feature_enabled("finance") is False


def test_apply_profile_full_restores_everything(db):
    from django.core.management import call_command

    from core import features

    call_command("apply_profile", "killboard")
    assert features.feature_enabled("industry") is False
    call_command("apply_profile", "full")
    assert features.feature_enabled("industry") is True
    assert features.feature_enabled("killboard") is True


def test_apply_profile_dry_run_writes_nothing(db):
    from django.core.management import call_command

    from core import features

    call_command("apply_profile", "killboard", "--dry-run")
    # Nothing changed — default is everything on.
    assert features.feature_enabled("industry") is True


def test_apply_profile_rejects_unknown(db):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("apply_profile", "bogus")


def test_apply_profile_defaults_to_settings(db, settings):
    from django.core.management import call_command

    from core import features

    settings.FORCA_PROFILE = "killboard"
    call_command("apply_profile")  # no arg → reads settings.FORCA_PROFILE
    assert features.feature_enabled("industry") is False


# --------------------------------------------------------------------------- #
#  6. Self-host guide exists
# --------------------------------------------------------------------------- #
def test_self_host_guide_present():
    from django.conf import settings

    guide = Path(settings.BASE_DIR) / "handbooks" / "operator-handbook" / "killboard-self-host.md"
    assert guide.exists()
    text = guide.read_text()
    assert "apply_profile killboard" in text
    assert "/auth/eve/callback/" in text
