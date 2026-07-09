"""Phase 7 tests: data deletion, retention, stats, security headers, audit."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit.models import AuditLog, DataRetentionPolicy
from apps.admin_audit.services import enforce_retention
from apps.characters.models import CharacterSkillSnapshot
from apps.identity.services import delete_user_data
from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import CombatMetric, Killmail
from apps.killboard.stats import rebuild_corp_metrics
from apps.sso.models import AuthToken, EveCharacter


@pytest.mark.django_db
def test_delete_user_data_keeps_killmails(sde, user):
    character = EveCharacter.objects.create(character_id=1001, user=user, name="P", is_main=True)
    CharacterSkillSnapshot.objects.create(character=character, skills={}, is_latest=True)
    tok = AuthToken(character=character)
    tok.refresh_token = "r"
    tok.save()
    # A killmail referencing this character (public fact) must be retained.
    ingest_killmail(
        555,
        "h",
        body={
            "killmail_id": 555,
            "killmail_time": "2026-06-20T10:00:00Z",
            "solar_system_id": 30002053,
            "victim": {"character_id": 1001, "corporation_id": 98000001, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 9}],
        },
    )

    summary = delete_user_data(user)
    assert summary["characters"] == 1
    assert CharacterSkillSnapshot.objects.filter(character=character).count() == 0
    assert AuthToken.objects.filter(character=character).count() == 0
    character.refresh_from_db()
    assert character.user_id is None  # detached
    assert Killmail.objects.filter(killmail_id=555).exists()  # public fact retained
    assert AuditLog.objects.filter(action="user.data_deleted").exists()


@pytest.mark.django_db
def test_enforce_retention_prunes_old_nonlatest(character):
    DataRetentionPolicy.objects.create(
        data_class=DataRetentionPolicy.DataClass.SKILL_SNAPSHOT, retention_days=365
    )
    old = CharacterSkillSnapshot.objects.create(
        character=character, skills={}, is_latest=False, as_of=timezone.now() - timedelta(days=400)
    )
    latest = CharacterSkillSnapshot.objects.create(character=character, skills={}, is_latest=True)
    enforce_retention()
    assert not CharacterSkillSnapshot.objects.filter(pk=old.pk).exists()
    assert CharacterSkillSnapshot.objects.filter(pk=latest.pk).exists()


@pytest.mark.django_db
def test_rebuild_corp_metrics(sde):
    ingest_killmail(
        1,
        "h",
        body={
            "killmail_id": 1,
            "killmail_time": timezone.now().isoformat(),
            "solar_system_id": 30002053,
            "victim": {"corporation_id": 98000001, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 9, "corporation_id": 99}],
        },
    )  # a loss
    ingest_killmail(
        2,
        "h",
        body={
            "killmail_id": 2,
            "killmail_time": timezone.now().isoformat(),
            "solar_system_id": 30002053,
            "victim": {"corporation_id": 77, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 9, "corporation_id": 98000001}],
        },
    )  # a kill
    rebuild_corp_metrics()
    metric = CombatMetric.objects.get(
        entity_type=CombatMetric.EntityType.CORPORATION, entity_id=98000001, window="all"
    )
    assert metric.kills == 1
    assert metric.losses == 1


@pytest.mark.django_db
def test_delete_data_view_logs_out(client, user):
    EveCharacter.objects.create(character_id=2002, user=user, name="X", is_main=True)
    client.force_login(user)
    resp = client.post("/privacy/delete/")
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.characters.count() == 0  # detached


@pytest.mark.django_db
def test_audit_view_permissions(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    assert client.get("/ops/audit/").status_code == 302  # anon

    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/ops/audit/").status_code == 403

    director = django_user_model.objects.create(username="d")
    RoleAssignment.objects.create(user=director, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(director)
    assert client.get("/ops/audit/").status_code == 200


@pytest.mark.django_db
def test_security_headers_present(client):
    # Test settings have DEBUG=False, so the security middleware applies.
    resp = client.get("/")
    csp = resp["Content-Security-Policy"]
    assert "Content-Security-Policy" in resp
    assert resp["X-Content-Type-Options"] == "nosniff"
    # Alpine.js evaluates directives via the Function constructor, which CSP
    # blocks without 'unsafe-eval'; without this the interactive UI silently dies.
    assert "'unsafe-eval'" in csp
