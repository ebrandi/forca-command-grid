"""Campaign Command performance / query-budget suite (design doc 12 §6, requirements §17, AC 24).

Query-count pins are the enforceable proxy for efficiency in this codebase (no load harness exists,
per house precedent in ``tests/test_readiness_perf.py`` / ``tests/test_market_perf.py``). A seeded
volume world (300 pilots, 30 campaigns, 300 objectives, plus one deliberately heavy campaign) proves
each hot surface executes a **fixed** number of queries independent of row volume — an N+1 regression
in a view or the refresh beat trips the ceiling.

Ceilings are the doc §6 budgets, asserted with ``django_assert_max_num_queries`` (a ceiling, not an
exact pin, so unrelated middleware/session churn never flakes the test while a real regression still
trips it). Volume is built once per test via the ``seeded_world`` fixture with ``bulk_create``
(scaffolding rows, not behaviour under test — the documented seeding exception).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.campaigns import metrics, services
from apps.campaigns.metrics.base import Measurement, MetricSource
from apps.campaigns.models import (
    Campaign,
    CampaignActivity,
    CampaignDependency,
    CampaignEvidence,
    Issue,
    Milestone,
    Objective,
    ObjectiveSample,
    Risk,
    Workstream,
)
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

CS = Campaign.Status
OS = Objective.ObjectiveStatus
VIS = Campaign.Visibility

# Doc 12 §6 query budgets.
PORTFOLIO_BUDGET = 15        # ≤ the doc's fixed strip/page/count set, independent of row volume
DETAIL_WARMED_BUDGET = 20    # participation panel served from cache on the second request
DETAIL_COLD_BUDGET = 25      # first request also fills the participation cache
EXPLAIN_BUDGET = 20
REFRESH_CAMPAIGN_BUDGET = 60  # one refresh_campaign over 12 auto objectives — no per-objective N+1

N_PILOTS = 300
N_CAMPAIGNS = 30
OBJECTIVES_PER_CAMPAIGN = 10


class _PerfSource(MetricSource):
    """A registry source with no backing reads — its value is constant, so refresh cost is pure
    campaign query volume (what the budget measures)."""

    key = "test.perf"
    label = "Test perf"
    unit = "count"
    data_class = "default"
    params_schema: list = []

    def measure(self, params):
        return Measurement(value=Decimal(7), as_of=timezone.now(), detail={})


@pytest.fixture
def seeded_world(django_user_model):
    """300 pilots + 30 members-tier ACTIVE campaigns × 10 objectives, ~90 milestones, ~60
    dependencies, samples + activity, and one deliberately heavy campaign (12 objectives, 9
    workstreams, 20 activities, risks/issues/evidence/dependencies) for the detail budget.

    A ``test.perf`` metric source is registered so the refresh sweep has real auto objectives to
    measure; it is unregistered on teardown."""
    src = _PerfSource()
    metrics.register(src)

    member_role = ensure_role(rbac.ROLE_MEMBER)
    User = django_user_model

    # 300 pilots: users + corp-member characters + member role rows (bulk).
    users = User.objects.bulk_create(
        [User(username=f"eve:perf{i}") for i in range(N_PILOTS)]
    )
    users = list(User.objects.filter(username__startswith="eve:perf").order_by("id"))
    EveCharacter.objects.bulk_create([
        EveCharacter(character_id=90_000_000 + i, user=users[i], name=f"perf{i}",
                     is_main=True, is_corp_member=True)
        for i in range(N_PILOTS)
    ])
    RoleAssignment.objects.bulk_create(
        [RoleAssignment(user=u, role=member_role) for u in users]
    )

    # 30 campaigns, ACTIVE members-tier, each commanded by a pool pilot.
    now = timezone.now()
    Campaign.objects.bulk_create([
        Campaign(
            name=f"Vol Campaign {i:02d}", status=CS.ACTIVE, visibility=VIS.MEMBERS,
            category=Campaign.Category.DEPLOYMENT, commander=users[i],
            start_at=now - timezone.timedelta(days=3),
            target_end_at=now + timezone.timedelta(days=30),
        )
        for i in range(N_CAMPAIGNS)
    ])
    campaigns = list(Campaign.objects.filter(name__startswith="Vol Campaign").order_by("id"))

    # 300 objectives (10/campaign; even index auto, odd manual), owners spread across the pool.
    objectives = []
    for ci, c in enumerate(campaigns):
        for k in range(OBJECTIVES_PER_CAMPAIGN):
            auto = k % 2 == 0
            objectives.append(Objective(
                campaign=c, title=f"Obj {ci}-{k}", status=OS.ACTIVE,
                owner=users[(ci * OBJECTIVES_PER_CAMPAIGN + k) % N_PILOTS],
                weight=1, baseline_value=Decimal(0), target_value=Decimal(100),
                metric_source="test.perf" if auto else "",
            ))
    Objective.objects.bulk_create(objectives)
    all_objectives = list(Objective.objects.filter(campaign__in=campaigns).order_by("id"))

    # ~90 milestones (3/campaign), ~300 samples (1/objective), ~150 activity rows.
    Milestone.objects.bulk_create([
        Milestone(campaign=c, title=f"MS {i}", due_at=now + timezone.timedelta(days=i % 20 + 1))
        for c in campaigns for i in range(3)
    ])
    ObjectiveSample.objects.bulk_create([
        ObjectiveSample(objective=o, value=Decimal(5), measured_at=now - timezone.timedelta(days=1))
        for o in all_objectives
    ])
    CampaignActivity.objects.bulk_create([
        CampaignActivity(campaign=c, verb="objective.progress", target_kind="objective")
        for c in campaigns for _ in range(5)
    ])
    # ~60 dependencies (2/campaign, external so no cycle walk cost). Each edge uses a distinct
    # ``from`` objective so the unique-per-pair constraint is satisfied.
    CampaignDependency.objects.bulk_create([
        CampaignDependency(campaign=c, from_kind="objective",
                           from_id=all_objectives[ci * OBJECTIVES_PER_CAMPAIGN + k].pk,
                           to_kind="external", to_id=0, note="ext")
        for ci, c in enumerate(campaigns) for k in range(2)
    ])

    # The heavy campaign for the detail budget: 12 objectives / 9 workstreams / 20 activities /
    # risks / issues / evidence / dependencies. Recognition on so participation warms a cache.
    big = Campaign.objects.create(
        name="Heavy Deployment", status=CS.ACTIVE, visibility=VIS.MEMBERS,
        category=Campaign.Category.DEPLOYMENT, commander=users[0],
        recognition_mode=Campaign.RecognitionMode.COUNTS, recognition_public=True,
        start_at=now - timezone.timedelta(days=3), target_end_at=now + timezone.timedelta(days=30),
    )
    workstreams = Workstream.objects.bulk_create([
        Workstream(campaign=big, name=f"WS {i}", key=f"ws{i}", lead=users[i]) for i in range(9)
    ])
    Objective.objects.bulk_create([
        Objective(campaign=big, title=f"Heavy Obj {i}", status=OS.ACTIVE, owner=users[i],
                  workstream=workstreams[i % 9], weight=1, baseline_value=Decimal(0),
                  target_value=Decimal(100),
                  metric_source="test.perf" if i % 2 == 0 else "")
        for i in range(12)
    ])
    Milestone.objects.bulk_create([
        Milestone(campaign=big, title=f"Heavy MS {i}", workstream=workstreams[i % 9],
                  due_at=now + timezone.timedelta(days=i + 1))
        for i in range(6)
    ])
    Risk.objects.bulk_create([
        Risk(campaign=big, description=f"risk {i}", severity=4) for i in range(5)
    ])
    Issue.objects.bulk_create([
        Issue(campaign=big, description=f"issue {i}", status=Issue.IssueStatus.OPEN)
        for i in range(4)
    ])
    CampaignEvidence.objects.bulk_create([
        CampaignEvidence(campaign=big, attached_kind="campaign", attached_id=big.pk,
                         url=f"https://ref.example/{i}", added_by=users[0])
        for i in range(4)
    ])
    CampaignActivity.objects.bulk_create([
        CampaignActivity(campaign=big, verb="status.changed", target_kind="campaign")
        for _ in range(20)
    ])
    big_obj_ids = list(big.objectives.order_by("id").values_list("pk", flat=True))
    CampaignDependency.objects.bulk_create([
        CampaignDependency(campaign=big, from_kind="objective", from_id=big_obj_ids[i],
                           to_kind="external", to_id=0, note="e")
        for i in range(3)
    ])

    officer = User.objects.create(username="eve:perf_officer")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    EveCharacter.objects.create(character_id=95_000_001, user=officer, name="perfoff",
                                is_main=True, is_corp_member=True)
    member = users[42]

    yield {"officer": officer, "member": member, "campaigns": campaigns, "big": big,
           "objectives": all_objectives}

    metrics.unregister("test.perf")


# ===========================================================================
#  Portfolio query budget — fixed cost independent of row volume (doc 12 §6)
# ===========================================================================
def test_portfolio_query_budget(client, django_user_model, seeded_world):
    """``GET /campaigns/`` as an officer over 30 campaigns must execute a fixed query set (auth/role
    + feature map + the two strip aggregates + one page query with ``select_related('commander')`` +
    pagination count) — no query may scale with row count (doc 12 §6, AC 24).

    A throwaway request first warms the app-global middleware/feature/corp caches so the measurement
    is the portfolio's own query cost, not one-time process warm-up.

    KNOWN FAIL — surfaces a real N+1: ``views.portfolio`` calls ``_next_milestone(c)`` inside the
    per-row loop (``apps/campaigns/views.py:259``), issuing one ``campaigns_milestone`` query per
    row (25 on a full page). Left failing at the doc ceiling per the perf regression policy so the
    view is fixed (prefetch/annotate the next milestone) rather than the budget weakened."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.force_login(seeded_world["officer"])
    client.get(reverse("campaigns:index"))  # warm global caches, not measured
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(reverse("campaigns:index"))
    assert resp.status_code == 200

    milestone_queries = [q for q in ctx.captured_queries if "campaigns_milestone" in q["sql"]]
    assert len(milestone_queries) <= 1, (
        f"portfolio issues {len(milestone_queries)} milestone queries (one per row) — "
        f"_next_milestone() in views.portfolio is an N+1; prefetch it. "
        f"total queries={len(ctx.captured_queries)}"
    )
    assert len(ctx.captured_queries) <= PORTFOLIO_BUDGET


def test_portfolio_pagination_capped_at_volume(client, django_user_model, seeded_world):
    """At volume the portfolio is paginated at 25/page: page 1 is full, page 2 exists and carries
    the remainder — a member never receives all 30+ rows in one unbounded response (T18)."""
    client.force_login(seeded_world["member"])
    page1 = client.get(reverse("campaigns:index")).content
    assert b"Page 1 of 2" in page1
    page2 = client.get(reverse("campaigns:index"), {"page": "2"})
    assert page2.status_code == 200
    assert b"Page 2 of 2" in page2.content
    # The member sees the 30 members-tier campaigns + the heavy one = 31 → 2 pages of ≤25.
    assert b"31 total" in page1


# ===========================================================================
#  Campaign detail query budget — warmed ≤20 / cold ≤25 (doc 12 §6)
# ===========================================================================
def test_campaign_detail_query_budget_warmed_and_cold(
    client, django_user_model, django_assert_max_num_queries, seeded_world
):
    """The heavy campaign's detail page. Doc 12 §6 defines *cold* as the participation cache
    unfilled (≤25) and *warmed* as served from that cache (≤20) — so a throwaway request first warms
    the app-global middleware/feature/corp caches (a one-time process cost every page pays, not a
    campaign-view cost), then the participation cache is busted to measure the true cold path.
    Objective/workstream/milestone/risk/issue/dependency/activity slices are each a single
    ``select_related``/``prefetch``ed query, so neither request scales with child count."""
    big = seeded_world["big"]
    client.force_login(seeded_world["member"])
    url = reverse("campaigns:detail", args=[big.pk])

    client.get(url)  # warm app-global caches (feature map, nav, corp config) — not measured
    services.bust_participation(big)  # cold = participation cache unfilled (doc 12 §6 definition)
    with django_assert_max_num_queries(DETAIL_COLD_BUDGET):
        assert client.get(url).status_code == 200
    with django_assert_max_num_queries(DETAIL_WARMED_BUDGET):
        assert client.get(url).status_code == 200


def test_explain_query_budget(client, django_user_model, django_assert_max_num_queries,
                              seeded_world):
    """The progress-explanation table decomposes every objective without an N+1 — one bounded query
    set for the heavy campaign's 12 objectives (doc 12 §6, doc 10 §6.4)."""
    big = seeded_world["big"]
    client.force_login(seeded_world["member"])
    url = reverse("campaigns:explain", args=[big.pk])
    client.get(url)  # warm app-global caches — not measured
    with django_assert_max_num_queries(EXPLAIN_BUDGET):
        assert client.get(url).status_code == 200


def test_pilot_panel_query_budget(client, django_user_model, django_assert_max_num_queries,
                                  seeded_world):
    """The pilot Command-Center panel builds from ``visible_campaigns`` with a bounded query set
    even when the pilot owns objectives across the volume world (doc 10 §6.5)."""
    member = seeded_world["member"]
    with django_assert_max_num_queries(EXPLAIN_BUDGET):
        panel = services.pilot_panel(member)
    assert panel["has_content"] is True


# ===========================================================================
#  Metric refresh sweep — bounded per campaign, exact sample count, idempotent (doc 12 §6)
# ===========================================================================
def test_refresh_campaign_query_budget(client, django_user_model, django_assert_max_num_queries,
                                       seeded_world):
    """One ``refresh_campaign`` over the heavy campaign's auto objectives runs a bounded query set —
    a per-objective N+1 in the beat would blow this ceiling. Sources are faked (constant), so the
    count is pure campaign query volume."""
    big = seeded_world["big"]
    big.refresh_from_db()
    with django_assert_max_num_queries(REFRESH_CAMPAIGN_BUDGET):
        services.refresh_campaign(big)


def test_refresh_sweep_writes_one_sample_per_due_objective_and_is_idempotent(
    django_user_model, seeded_world
):
    """The sweep over the 30-campaign world writes exactly one new sample per due auto objective;
    an immediate second run writes zero (the volume idempotency proof of doc 12 §6). Auto
    objectives are the even-indexed half → 5 per campaign across 30 campaigns + 6 on the heavy
    campaign."""
    due_auto = Objective.objects.filter(status=OS.ACTIVE).exclude(metric_source="").count()
    before = ObjectiveSample.objects.count()

    first = services.run_metric_refresh()
    after_first = ObjectiveSample.objects.count()
    assert first["refreshed"] == due_auto
    assert after_first - before == due_auto  # exactly one sample per due objective

    second = services.run_metric_refresh()
    assert second["refreshed"] == 0
    assert second["skipped_fresh"] == due_auto
    assert ObjectiveSample.objects.count() == after_first  # no duplicate samples on re-run
