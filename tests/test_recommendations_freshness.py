"""data_freshness reflects each evaluator's source as_of, not the engine run time."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.utils import timezone

from apps.killboard.models import Killmail

STALE = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
RIFTER = 587


def _home_loss(km_id, *, as_of):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}",
        killmail_time=timezone.now(), solar_system_id=30000142,
        victim_ship_type_id=RIFTER, involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
    )
    # as_of is a ProvenanceMixin field with a now() default; force it stale.
    Killmail.objects.filter(pk=km.pk).update(as_of=as_of)
    return km


@pytest.mark.django_db
def test_combat_loss_freshness_is_the_killmail_as_of(sde):
    from apps.recommendations.engine import eval_combat_loss_pattern

    for i in range(3):  # threshold is 3 of the same ship in-window
        _home_loss(7000 + i, as_of=STALE)

    drafts = eval_combat_loss_pattern()
    assert drafts, "expected a combat-loss-pattern recommendation"
    assert drafts[0]["data_freshness"] == STALE  # source freshness, not now()


@pytest.mark.django_db
def test_freshness_defaults_to_now_without_a_source():
    from apps.recommendations.engine import _draft

    before = timezone.now()
    d = _draft(
        "stock_shortage", subject_type="type", subject_id=1,
        message_key="stock_shortage.message",
        message_params={"type_name": "Rifter", "current": 1, "target": 2, "deficit": 1},
        logic_key="stock_shortage.logic",
        inputs={}, confidence="high", severity=1,
    )
    assert d["data_freshness"] >= before  # defaulted to ~now


@pytest.mark.django_db
def test_source_as_of_empty_queryset_is_now():
    from apps.recommendations.engine import _killmails_as_of

    before = timezone.now()
    assert _killmails_as_of() >= before  # no killmails -> now()
