"""Campaign Command Phase 3 background-job tests (design doc 08).

The three beats (``refresh_metrics`` / ``sweep_deadlines`` / ``housekeeping``) through their service
bodies: refresh idempotency + the min-interval gate, ``measurement_paused`` and the manual/auto
overwrite semantics (doc 04 §3.2), the target-reached prompt (activity row, no status change), the
``cache.add`` lock, feature-gated early return, the bucketed deadline idempotency keys, the manual
staleness nudge, and the retention prune rules. House style: ``pytest.mark.django_db``, a registered
fake source so a measurement value is fully controllable, real ``Alert`` rows asserted in-DB.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.campaigns import metrics, services, tasks
from apps.campaigns.metrics.base import Measurement, MetricSource
from apps.campaigns.models import (
    Campaign,
    CampaignActivity,
    Objective,
    ObjectiveSample,
)
from apps.identity.models import RoleAssignment
from apps.pingboard.models import Alert
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

CS = Campaign.Status
OS = Objective.ObjectiveStatus


# --------------------------------------------------------------------------- #
#  Fixtures & helpers
# --------------------------------------------------------------------------- #
class _FakeSource(MetricSource):
    """A registry source whose measured value the test controls directly."""

    key = "test.fake"
    label = "Test fake"
    unit = "count"
    data_class = "default"
    params_schema = []

    def __init__(self):
        self.value = Decimal(10)

    def measure(self, params):
        return Measurement(value=self.value, as_of=timezone.now(), detail={})


@pytest.fixture
def fake_source():
    src = _FakeSource()
    metrics.register(src)
    yield src
    metrics.unregister("test.fake")


def _member(django_user_model, username, *role_keys):
    u = django_user_model.objects.create(username=username)
    for key in role_keys:
        RoleAssignment.objects.create(user=u, role=ensure_role(key))
    EveCharacter.objects.create(
        character_id=abs(hash(username)) % 2_000_000_000, user=u, name=username,
        is_main=True, is_corp_member=True,
    )
    return u


def _active_campaign(**kwargs):
    now = timezone.now()
    defaults = dict(
        name=kwargs.pop("name", "Deployment"), status=CS.ACTIVE,
        start_at=now - timedelta(days=2), target_end_at=now + timedelta(days=30),
        visibility=Campaign.Visibility.MEMBERS,
    )
    defaults.update(kwargs)
    return Campaign.objects.create(**defaults)


def _auto_objective(campaign, source_key="test.fake", *, target=20, **kwargs):
    return Objective.objects.create(
        campaign=campaign, title=kwargs.pop("title", "Auto objective"),
        metric_source=source_key, baseline_value=Decimal(0), target_value=Decimal(target),
        status=kwargs.pop("status", OS.ACTIVE), **kwargs,
    )


# --------------------------------------------------------------------------- #
#  refresh_metrics — writes, idempotency, gating
# --------------------------------------------------------------------------- #
def test_refresh_writes_value_sample_and_recomputes(fake_source):
    c = _active_campaign()
    o = _auto_objective(c)
    fake_source.value = Decimal(15)

    summary = services.run_metric_refresh()

    o.refresh_from_db()
    assert o.current_value == Decimal(15)
    assert o.measurement_source == "auto"
    assert o.samples.count() == 1
    assert summary["refreshed"] == 1
    c.refresh_from_db()
    assert c.progress_pct == 75  # 15 / 20


def test_refresh_second_run_is_noop_skipped_fresh(fake_source):
    c = _active_campaign()
    o = _auto_objective(c)
    services.run_metric_refresh()
    summary = services.run_metric_refresh()
    assert summary["refreshed"] == 0
    assert summary["skipped_fresh"] == 1
    assert o.samples.count() == 1  # no duplicate sample


def test_refresh_records_automation_activity_only_on_change(fake_source):
    c = _active_campaign()
    o = _auto_objective(c)
    fake_source.value = Decimal(15)
    services.run_metric_refresh()
    # Age the objective past the interval, re-measure the SAME value → no new activity row.
    Objective.objects.filter(pk=o.pk).update(measured_at=timezone.now() - timedelta(minutes=20))
    services.run_metric_refresh()

    rows = CampaignActivity.objects.filter(
        campaign=c, verb="objective.progress", target_id=o.pk, source="automation"
    )
    assert rows.count() == 1  # only the first (changing) measure wrote activity
    assert o.samples.count() == 2  # but a sample is appended every successful measure


def test_paused_objective_is_skipped(fake_source):
    c = _active_campaign()
    o = _auto_objective(c, measurement_paused=True)
    summary = services.run_metric_refresh()
    o.refresh_from_db()
    assert o.current_value is None
    assert summary["refreshed"] == 0


def test_unknown_source_is_skipped_not_fatal():
    c = _active_campaign()
    o = _auto_objective(c, source_key="test.retired")
    summary = services.run_metric_refresh()
    o.refresh_from_db()
    assert o.current_value is None
    assert summary["errors"] == 0  # unknown source is logged + skipped, never an error


def test_source_failure_isolated_keeps_last_value(fake_source, monkeypatch):
    c = _active_campaign()
    o = _auto_objective(c)
    monkeypatch.setattr(fake_source, "measure", lambda params: (_ for _ in ()).throw(RuntimeError()))
    summary = services.run_metric_refresh()
    o.refresh_from_db()
    assert o.current_value is None  # untouched
    assert o.samples.count() == 0
    assert summary["errors"] == 1


def test_measure_objective_single_path(fake_source):
    c = _active_campaign()
    o = _auto_objective(c)
    fake_source.value = Decimal(12)

    assert services.measure_objective(o) is True  # value written
    o.refresh_from_db()
    assert o.current_value == Decimal(12)
    assert services.measure_objective(o) is False  # inside the min interval → skipped


def test_measure_objective_skips_paused(fake_source):
    c = _active_campaign()
    o = _auto_objective(c, measurement_paused=True)
    assert services.measure_objective(o) is False
    o.refresh_from_db()
    assert o.current_value is None


# --------------------------------------------------------------------------- #
#  Manual/auto precedence (doc 04 §3.2)
# --------------------------------------------------------------------------- #
def test_manual_value_preserved_while_paused(fake_source, django_user_model):
    u = _member(django_user_model, "eve:officer", rbac.ROLE_OFFICER)
    c = _active_campaign()
    o = _auto_objective(c)
    services.update_manual_value(o, u, "7", "bridging an ESI outage")
    Objective.objects.filter(pk=o.pk).update(measurement_paused=True)
    fake_source.value = Decimal(15)

    services.run_metric_refresh()

    o.refresh_from_db()
    assert o.current_value == Decimal(7)  # never overwritten while paused
    assert o.measurement_source == "manual"


def test_manual_value_overwritten_by_refresh_when_not_paused(fake_source, django_user_model):
    u = _member(django_user_model, "eve:officer2", rbac.ROLE_OFFICER)
    c = _active_campaign()
    o = _auto_objective(c)
    services.update_manual_value(o, u, "7", "a manual correction")
    # A manual entry stamps measured_at=now; age it past the interval so the sweep re-measures.
    Objective.objects.filter(pk=o.pk).update(measured_at=timezone.now() - timedelta(minutes=20))
    fake_source.value = Decimal(15)

    services.run_metric_refresh()

    o.refresh_from_db()
    assert o.current_value == Decimal(15)
    assert o.measurement_source == "auto"


# --------------------------------------------------------------------------- #
#  Target-reached prompt (doc 08 §3 trigger 1)
# --------------------------------------------------------------------------- #
def test_target_reached_records_prompt_without_status_change(fake_source):
    c = _active_campaign()
    o = _auto_objective(c, target=20)
    fake_source.value = Decimal(25)  # exceeds the target

    services.run_metric_refresh()

    o.refresh_from_db()
    assert o.progress_pct == 100
    assert o.status == OS.ACTIVE  # automation never auto-closes the objective
    assert CampaignActivity.objects.filter(
        campaign=c, verb="objective.target_reached", target_id=o.pk, source="automation"
    ).exists()


# --------------------------------------------------------------------------- #
#  Task wrappers: lock + feature gate
# --------------------------------------------------------------------------- #
def test_refresh_lock_prevents_overlap(fake_source):
    cache.add("campaigns:lock:refresh_metrics", "1", 600)
    try:
        assert tasks.refresh_metrics() == {"status": "already_running"}
    finally:
        cache.delete("campaigns:lock:refresh_metrics")


def test_all_beats_early_return_when_feature_disabled(monkeypatch):
    monkeypatch.setattr("core.features.feature_enabled", lambda key: False)
    assert tasks.refresh_metrics() == {"status": "feature_disabled"}
    assert tasks.sweep_deadlines() == {"status": "feature_disabled"}
    assert tasks.housekeeping() == {"status": "feature_disabled"}


# --------------------------------------------------------------------------- #
#  sweep_deadlines — bucketed idempotency + manual staleness
# --------------------------------------------------------------------------- #
def _campaign_alerts():
    return Alert.objects.filter(source_service="campaigns")


def test_sweep_emits_deadline_bucket_and_is_idempotent(django_user_model):
    owner = _member(django_user_model, "eve:owner", rbac.ROLE_MEMBER)
    c = _active_campaign(commander=owner)
    o = Objective.objects.create(
        campaign=c, title="Ship it", owner=owner, status=OS.ACTIVE,
        due_at=timezone.now() + timedelta(hours=20),  # inside the 24h bucket
    )

    first = services.run_deadline_sweep()
    count_after_first = _campaign_alerts().count()
    second = services.run_deadline_sweep()

    assert _campaign_alerts().count() == count_after_first  # no duplicate on re-run
    assert second["suppressed"] >= 1
    assert Alert.objects.filter(idempotency_key=f"campaigns:due:objective:{o.pk}:24h").exists()
    assert first["due_soon"] >= 1


def test_sweep_overdue_bucket(django_user_model):
    owner = _member(django_user_model, "eve:owner2", rbac.ROLE_MEMBER)
    c = _active_campaign(commander=owner)
    o = Objective.objects.create(
        campaign=c, title="Late", owner=owner, status=OS.ACTIVE,
        due_at=timezone.now() - timedelta(days=1),
    )
    summary = services.run_deadline_sweep()
    assert summary["overdue"] >= 1
    assert Alert.objects.filter(idempotency_key=f"campaigns:due:objective:{o.pk}:overdue").exists()


def test_sweep_nudges_stale_manual_objective(django_user_model):
    owner = _member(django_user_model, "eve:owner3", rbac.ROLE_MEMBER)
    c = _active_campaign(commander=owner)
    o = Objective.objects.create(
        campaign=c, title="Manual", owner=owner, status=OS.ACTIVE, metric_source="",
    )
    Objective.objects.filter(pk=o.pk).update(measured_at=timezone.now() - timedelta(days=30))

    summary = services.run_deadline_sweep()

    assert summary["manual_stale"] >= 1
    assert Alert.objects.filter(
        source_service="campaigns", idempotency_key__startswith=f"campaigns:manual:{o.pk}:"
    ).exists()


# --------------------------------------------------------------------------- #
#  housekeeping — retention prunes
# --------------------------------------------------------------------------- #
def test_housekeeping_prunes_old_samples_keeping_newest():
    c = _active_campaign()
    o = _auto_objective(c)
    now = timezone.now()
    older = ObjectiveSample.objects.create(objective=o, value=1, measured_at=now - timedelta(days=400))
    newest_old = ObjectiveSample.objects.create(objective=o, value=2, measured_at=now - timedelta(days=300))

    summary = services.run_housekeeping()

    ids = set(ObjectiveSample.objects.values_list("pk", flat=True))
    assert newest_old.pk in ids  # the newest per objective is always kept, even when old
    assert older.pk not in ids
    assert summary["samples_pruned"] == 1


def test_housekeeping_prunes_archived_activity_only(django_user_model):
    old = timezone.now() - timedelta(days=400)
    active_c = _active_campaign()
    archived_c = Campaign.objects.create(name="Wrapped", status=CS.ARCHIVED)
    a_active = CampaignActivity.objects.create(campaign=active_c, verb="x")
    a_archived = CampaignActivity.objects.create(campaign=archived_c, verb="y")
    CampaignActivity.objects.filter(pk__in=[a_active.pk, a_archived.pk]).update(created_at=old)

    summary = services.run_housekeeping()

    assert CampaignActivity.objects.filter(pk=a_active.pk).exists()  # active campaign untouched
    assert not CampaignActivity.objects.filter(pk=a_archived.pk).exists()
    assert summary["activity_pruned"] == 1


def test_housekeeping_prunes_archived_samples_on_tighter_clock():
    # Archived campaigns keep samples only 30 days (well inside the global 180), newest per
    # objective always retained for the report sparkline (#41).
    now = timezone.now()
    archived = _active_campaign(status=CS.ARCHIVED)
    o = _auto_objective(archived)
    older = ObjectiveSample.objects.create(objective=o, value=1, measured_at=now - timedelta(days=60))
    newest = ObjectiveSample.objects.create(objective=o, value=2, measured_at=now - timedelta(days=40))

    summary = services.run_housekeeping()

    ids = set(ObjectiveSample.objects.values_list("pk", flat=True))
    assert newest.pk in ids       # newest per objective is always kept
    assert older.pk not in ids    # the >30-day archived sample is pruned
    assert summary["archived_samples_pruned"] == 1
    assert summary["samples_pruned"] == 0  # the global 180-day rule leaves both alone


# --------------------------------------------------------------------------- #
#  Beat lock: token-guarded release + record_sync stamp
# --------------------------------------------------------------------------- #
def test_stale_token_release_leaves_successor_lock():
    # An overran run must not delete a successor's freshly re-acquired lock (#33).
    from apps.campaigns.tasks import _REFRESH_LOCK, _release_lock

    cache.set(_REFRESH_LOCK, "successor-token", 600)
    _release_lock(_REFRESH_LOCK, "stale-token")            # we no longer own the lock
    assert cache.get(_REFRESH_LOCK) == "successor-token"   # successor's lock survives
    _release_lock(_REFRESH_LOCK, "successor-token")        # the true owner releases cleanly
    assert cache.get(_REFRESH_LOCK) is None


def test_refresh_beat_stamps_record_sync(fake_source, monkeypatch):
    from apps.admin_audit.health import _last_sync
    from apps.campaigns.tasks import _REFRESH_LOCK

    monkeypatch.setattr("core.features.feature_enabled", lambda key: True)
    cache.delete(_REFRESH_LOCK)
    c = _active_campaign()
    _auto_objective(c)
    tasks.refresh_metrics()
    assert _last_sync("campaigns_refresh_metrics") is not None  # observability stamp written (#42)
