"""Native admin console for CEO/Directors — roles, doctrines, content, maintenance.

Replaces the Django /admin for day-to-day corp configuration; everything is
RBAC-gated and audit-logged.
"""
from __future__ import annotations

import pytest

from apps.admin_audit.models import AuditLog
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.onboarding.models import GlossaryTerm
from apps.sso.services import ensure_role
from core import rbac

# Uses types present in the test SDE fixture (Rifter 587, AutoCannon 484, DC 2046).
RIFTER_EFT = "[Rifter, Test Fit]\n200mm AutoCannon I\nDamage Control I"


def _user(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    return user


# --- Access control ----------------------------------------------------------
@pytest.mark.django_db
def test_console_is_officer_accessible_with_director_cards_hidden(client, django_user_model, sde):
    # The hub is the single home for management tasks, so officers can open it —
    # but director-only cards are hidden for them (each destination still enforces
    # its own role server-side).
    assert client.get("/ops/admin/").status_code == 302  # anon -> login

    client.force_login(_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    officer_html = client.get("/ops/admin/").content.decode()
    assert client.get("/ops/admin/").status_code == 200            # officer can see the hub
    assert "/ops/admin/members/" not in officer_html               # director-only card hidden
    assert "/ops/audit/" not in officer_html                 # director-only card hidden
    assert "/srp/settings/" in officer_html                        # officer-safe card shown

    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    director_html = client.get("/ops/admin/").content.decode()
    assert "/ops/admin/members/" in director_html                  # director sees the full set
    assert "/ops/audit/" in director_html


# --- Members & roles ---------------------------------------------------------
@pytest.mark.django_db
def test_director_grants_and_revokes_roles(client, django_user_model, sde):
    director = _user(django_user_model, "ceo", rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "pilot", rbac.ROLE_MEMBER)
    client.force_login(director)

    # Grant officer (button value = role; 4.17 contract).
    client.post(f"/ops/admin/members/{target.id}/role/", {"grant": "officer"})
    assert target.role_assignments.filter(role__key="officer").exists()
    assert AuditLog.objects.filter(action="role.granted").exists()

    # Revoke it.
    client.post(f"/ops/admin/members/{target.id}/role/", {"revoke": "officer"})
    assert not target.role_assignments.filter(role__key="officer").exists()


@pytest.mark.django_db
def test_cannot_grant_admin_or_strip_last_director(client, django_user_model, sde):
    director = _user(django_user_model, "ceo", rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "pilot", rbac.ROLE_MEMBER)
    client.force_login(director)

    # 'admin' is not manageable from the UI (no superuser minting).
    assert client.post(f"/ops/admin/members/{target.id}/role/", {"grant": "admin"}).status_code == 403

    # The last Director cannot be demoted (lockout guard).
    client.post(f"/ops/admin/members/{director.id}/role/", {"revoke": "director"})
    assert director.role_assignments.filter(role__key="director").exists()


# --- Doctrine management -----------------------------------------------------
@pytest.mark.django_db
def test_officer_creates_doctrine_and_adds_fit_from_eft(client, django_user_model, sde):
    officer = _user(django_user_model, "fc", rbac.ROLE_OFFICER)
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    client.force_login(officer)

    resp = client.post("/ops/admin/doctrines/create/", {
        "name": "Rifter Roam", "category": cat.id, "priority": 80, "description": "tackle",
    })
    assert resp.status_code == 302
    doctrine = Doctrine.objects.get(name="Rifter Roam")
    assert doctrine.created_by == officer

    # Paste an EFT -> a fit is created with derived skill requirements.
    client.post(f"/ops/admin/doctrines/{doctrine.pk}/fit/add/", {"eft": RIFTER_EFT})
    fit = DoctrineFit.objects.get(doctrine=doctrine)
    assert fit.ship_type_id == 587  # Rifter
    assert fit.modules  # parsed modules stored

    # Update status, then delete.
    client.post(f"/ops/admin/doctrines/{doctrine.pk}/update/", {"name": "Rifter Roam", "status": "retired"})
    doctrine.refresh_from_db()
    assert doctrine.status == "retired"
    client.post(f"/ops/admin/doctrines/{doctrine.pk}/delete/")
    assert not Doctrine.objects.filter(pk=doctrine.pk).exists()


@pytest.mark.django_db
def test_member_cannot_manage_doctrines(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/ops/admin/doctrines/").status_code == 403


# --- Onboarding content ------------------------------------------------------
@pytest.mark.django_db
def test_officer_manages_milestones(client, django_user_model, sde):
    from apps.onboarding.models import OnboardingMilestone

    client.force_login(_user(django_user_model, "mo", rbac.ROLE_MEMBER, rbac.ROLE_OFFICER))
    client.post("/ops/admin/content/milestones/create/", {
        "title": "Register on voice comms", "category": "account", "check": "manual",
        "description": "Comms are mandatory for fleets.", "url": "https://discord.gg/x",
        "sort_order": "15", "active": "on",
    })
    m = OnboardingMilestone.objects.get(title="Register on voice comms")
    assert m.key == "register-on-voice-comms" and m.criteria == {} and m.url == "https://discord.gg/x"

    # Flip it to an auto check with params.
    client.post(f"/ops/admin/content/milestones/{m.pk}/update/", {
        "title": "Grant asset scopes", "category": "account", "check": "scopes",
        "scopes": "esi-assets.read_assets.v1, esi-skills.read_skills.v1",
        "sort_order": "15", "active": "on",
    })
    m.refresh_from_db()
    assert m.criteria == {"type": "scopes",
                          "scopes": ["esi-assets.read_assets.v1", "esi-skills.read_skills.v1"]}

    client.post(f"/ops/admin/content/milestones/{m.pk}/delete/")
    assert not OnboardingMilestone.objects.filter(pk=m.pk).exists()


@pytest.mark.django_db
def test_member_cannot_manage_milestones(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "mm", rbac.ROLE_MEMBER))
    resp = client.post("/ops/admin/content/milestones/create/", {"title": "X", "check": "manual"})
    assert resp.status_code == 403


@pytest.mark.django_db
def test_officer_manages_glossary(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    client.post("/ops/admin/content/glossary/create/", {"term": "Tackle", "definition": "Hold a target."})
    term = GlossaryTerm.objects.get(term="Tackle")
    client.post(f"/ops/admin/content/glossary/{term.id}/delete/")
    assert not GlossaryTerm.objects.filter(term="Tackle").exists()


# --- Maintenance -------------------------------------------------------------
@pytest.mark.django_db
def test_maintenance_enqueues_known_task(client, django_user_model, sde, monkeypatch):
    sent = {}
    from config import celery as celery_mod
    monkeypatch.setattr(celery_mod.app, "send_task", lambda name, *a, **k: sent.setdefault("name", name))

    client.force_login(_user(django_user_model, "ceo", rbac.ROLE_DIRECTOR))
    resp = client.post("/ops/admin/maintenance/recommendations/")
    assert resp.status_code == 302
    assert sent["name"] == "recommendations.run"
    # Unknown action is rejected.
    assert client.post("/ops/admin/maintenance/bogus/").status_code == 403
