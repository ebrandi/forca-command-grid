"""LOG-1 (roadmap 3.2) — overdue-haul sweep & hauler nudges.

Overdue IN_PROGRESS hauls are auto-released back to the pool (unconditional); haulers get a
pre-deadline reminder and posters/former haulers learn when a haul is released (gated DMs).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.logistics.models import CourierContract as CC
from apps.logistics.services import sweep_overdue_hauls

pytestmark = pytest.mark.django_db


def _haul(django_user_model, *, deadline, status=CC.Status.IN_PROGRESS, hauler=True, reminder=None):
    poster = django_user_model.objects.create(username=f"poster{deadline.timestamp()}")
    hauler_u = django_user_model.objects.create(username=f"hauler{deadline.timestamp()}") if hauler else None
    return CC.objects.create(
        origin_name="Jita", dest_name="Amarr", status=status, deadline=deadline,
        created_by=poster, assigned_user=hauler_u,
        assigned_hauler_character_id=9001 if hauler else None, reminder_sent_at=reminder,
    )


def test_overdue_haul_is_released_to_pool(django_user_model):
    c = _haul(django_user_model, deadline=timezone.now() - timedelta(hours=1))
    assert sweep_overdue_hauls()["released"] == 1
    c.refresh_from_db()
    assert c.status == CC.Status.OUTSTANDING
    assert c.assigned_user_id is None and c.assigned_hauler_character_id is None
    assert c.reminder_sent_at is None


def test_reminder_fires_once_in_lead_window(django_user_model):
    c = _haul(django_user_model, deadline=timezone.now() + timedelta(hours=2))  # inside 6h lead
    assert sweep_overdue_hauls()["reminded"] == 1
    c.refresh_from_db()
    assert c.reminder_sent_at is not None and c.status == CC.Status.IN_PROGRESS
    assert sweep_overdue_hauls()["reminded"] == 0  # not re-reminded


def test_far_future_haul_untouched(django_user_model):
    c = _haul(django_user_model, deadline=timezone.now() + timedelta(days=3))
    assert sweep_overdue_hauls() == {"reminded": 0, "released": 0}
    c.refresh_from_db()
    assert c.status == CC.Status.IN_PROGRESS and c.reminder_sent_at is None


def test_non_in_progress_haul_ignored(django_user_model):
    c = _haul(django_user_model, deadline=timezone.now() - timedelta(hours=1), status=CC.Status.DELIVERED)
    assert sweep_overdue_hauls()["released"] == 0
    c.refresh_from_db()
    assert c.status == CC.Status.DELIVERED  # a delivered haul is never released


def test_reclaim_resets_deadline_so_not_instantly_reswept(client, django_user_model):
    from django.urls import reverse

    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role
    from core import rbac

    c = _haul(django_user_model, deadline=timezone.now() - timedelta(hours=1))
    sweep_overdue_hauls()  # release it back to the pool
    c.refresh_from_db()
    assert c.status == CC.Status.OUTSTANDING

    hauler = django_user_model.objects.create(username="reclaimer")
    RoleAssignment.objects.create(user=hauler, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9500, user=hauler, name="R", is_main=True,
                                is_corp_member=True)
    client.force_login(hauler)
    client.post(reverse("logistics:claim_contract", args=[c.pk]))
    c.refresh_from_db()
    assert c.status == CC.Status.IN_PROGRESS
    assert c.deadline > timezone.now()  # fresh future window, not the stale past deadline
    # The very next sweep must NOT re-release the freshly-claimed haul.
    assert sweep_overdue_hauls()["released"] == 0
    c.refresh_from_db()
    assert c.status == CC.Status.IN_PROGRESS


def test_release_notifies_poster_and_hauler(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr("apps.pingboard.services.emit_broadcast", lambda **kw: calls.append(kw))
    _haul(django_user_model, deadline=timezone.now() - timedelta(hours=1))
    sweep_overdue_hauls()
    object_ids = [c.get("source_object_id", "") for c in calls]
    assert any("haul_overdue_poster" in o for o in object_ids)
    assert any("haul_overdue_hauler" in o for o in object_ids)
