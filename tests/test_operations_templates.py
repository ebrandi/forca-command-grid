"""OPS-4 (roadmap 3.12) — recurring / templated strat-op schedules.

A template materialises real Operation instances on a weekly cadence (composition/SRP/deadline
copied), idempotently, and mirrors them onto the Pingboard calendar.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.operations.models import Operation, OperationTemplate, OperationTemplateSlot
from apps.operations.services import _template_occurrences, materialize_recurring_ops
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _template(**kw):
    defaults = dict(name="Weekly PvP", type="pvp", weekday=5, hour=20, minute=0,
                    lead_days=10, min_pilots=5, srp="corp", rsvp_offset_minutes=30, active=True)
    defaults.update(kw)
    t = OperationTemplate.objects.create(**defaults)
    OperationTemplateSlot.objects.create(template=t, ship_name="Ferox", role="dps",
                                         min_pilots=10, max_pilots=0, priority=1)
    OperationTemplateSlot.objects.create(template=t, ship_name="Scythe", role="logi",
                                         min_pilots=3, max_pilots=5, priority=2)
    return t


def test_occurrences_weekly_within_window():
    t = _template(lead_days=14)
    now = timezone.now()
    occ = _template_occurrences(t, now, now + dt.timedelta(days=14))
    assert 1 <= len(occ) <= 3
    for o in occ:
        assert o.weekday() == 5 and o.hour == 20 and o.minute == 0
        assert now <= o <= now + dt.timedelta(days=14)


def test_materialize_spawns_instances_with_config_and_slots():
    t = _template(lead_days=10)
    assert materialize_recurring_ops()["created"] >= 1
    op = Operation.objects.filter(recurring_template=t).first()
    assert op is not None
    assert op.name == "Weekly PvP" and op.srp == "corp" and op.min_pilots == 5
    assert op.status == Operation.Status.PLANNED
    assert op.rsvp_deadline == op.target_at - dt.timedelta(minutes=30)
    slots = list(op.ship_slots.order_by("priority"))
    assert len(slots) == 2
    assert slots[0].ship_name == "Ferox" and slots[0].max_pilots is None   # 0 → no cap
    assert slots[1].ship_name == "Scythe" and slots[1].max_pilots == 5


def test_materialize_is_idempotent():
    _template()
    n1 = materialize_recurring_ops()["created"]
    n2 = materialize_recurring_ops()["created"]
    assert n1 >= 1 and n2 == 0


def test_inactive_template_not_materialized():
    _template(active=False)
    assert materialize_recurring_ops()["created"] == 0
    assert Operation.objects.filter(recurring_template__isnull=False).count() == 0


def test_materialized_op_appears_on_calendar():
    _template()
    materialize_recurring_ops()
    from apps.pingboard.models import CalendarEvent
    assert CalendarEvent.objects.filter(source_system="operations").exists()


def test_officer_can_create_template_via_form(client, django_user_model):
    user = django_user_model.objects.create(username="officer")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(user)
    resp = client.post(reverse("operations:template_create"), {
        "name": "Friday roam", "type": "roam", "weekday": "4", "hour": "19", "minute": "30",
        "lead_days": "7", "min_pilots": "8", "srp": "corp", "rsvp_offset_minutes": "15",
        "duration_minutes": "90", "active": "1",
        "slot_ship": ["Ferox", "", "Scythe"], "slot_role": ["dps", "dps", "logi"],
        "slot_min": ["10", "1", "3"], "slot_max": ["0", "0", "0"],
        "slot_priority": ["1", "1", "2"],
    })
    assert resp.status_code == 302
    t = OperationTemplate.objects.get(name="Friday roam")
    assert t.weekday == 4 and t.hour == 19 and t.minute == 30 and t.active is True
    # blank ship row skipped → 2 slots
    assert t.slots.count() == 2


def test_template_pages_render(client, django_user_model):
    user = django_user_model.objects.create(username="off2")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(user)
    t = _template()
    assert client.get(reverse("operations:templates")).status_code == 200
    assert client.get(reverse("operations:template_create")).status_code == 200
    assert client.get(reverse("operations:template_edit", args=[t.pk])).status_code == 200


def test_templates_are_officer_only(client, django_user_model):
    from apps.sso.models import EveCharacter
    user = django_user_model.objects.create(username="member")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=42, user=user, name="M", is_main=True,
                                is_corp_member=True)
    client.force_login(user)
    # A member is redirected/denied from the officer templates surface.
    assert client.get(reverse("operations:templates")).status_code in (302, 403)
