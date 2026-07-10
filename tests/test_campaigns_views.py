"""Campaign Command view-layer tests (Phase 1 — design docs 07, 10).

Pins the security spine and the member/officer surfaces: the visibility chokepoint applied to
both the list and direct URLs (404 for invisibility, the no-existence-oracle rule), the
per-object role widening (a plain-member commander), the director-only approval gate, illegal
transitions rejected without a button, objective ownership on manual updates, verification
separation of duties, budget + sensitive-value stripping, nav gating, the htmx filter partial
and pagination. House style: ``client.force_login``, HTML-substring asserts, ``_user`` role
helpers built on ``apps.sso.services.ensure_role`` + ``core.rbac`` constants (mirrors
``tests/test_campaigns_services.py``).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.campaigns.models import Campaign, CampaignActivity, Objective
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac
from core.features import set_disabled

pytestmark = pytest.mark.django_db

CS = Campaign.Status
VIS = Campaign.Visibility
OS = Objective.ObjectiveStatus


@pytest.fixture(autouse=True)
def _clear_cache():
    """Feature-flag and config caches are process-global (LocMem) — clear around each test so a
    ``set_disabled`` in one test never bleeds into the next."""
    cache.clear()
    yield
    cache.clear()


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _user(django_user_model, username, *role_keys):
    u = django_user_model.objects.create(username=username)
    for key in role_keys:
        RoleAssignment.objects.create(user=u, role=ensure_role(key))
    return u


def _campaign(**kwargs) -> Campaign:
    kwargs.setdefault("name", "Deployment Readiness")
    kwargs.setdefault("status", CS.ACTIVE)
    kwargs.setdefault("visibility", VIS.MEMBERS)
    return Campaign.objects.create(**kwargs)


def _future():
    return timezone.now() + timezone.timedelta(days=30)


# --------------------------------------------------------------------------- #
#  Feature gate + auth
# --------------------------------------------------------------------------- #
def test_feature_off_404s_the_namespace(client, django_user_model):
    member = _user(django_user_model, "eve:m1", rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.get(reverse("campaigns:index")).status_code == 200
    set_disabled(["campaigns"])
    assert client.get(reverse("campaigns:index")).status_code == 404
    c = _campaign()
    assert client.get(reverse("campaigns:detail", args=[c.pk])).status_code == 404


def test_anonymous_redirected_to_login(client):
    resp = client.get(reverse("campaigns:index"))
    assert resp.status_code == 302
    assert reverse("campaigns:index") not in resp["Location"].split("?", 1)[0]


# --------------------------------------------------------------------------- #
#  Visibility chokepoint — list AND direct URL (doc 07 §1.4, T1/T2)
# --------------------------------------------------------------------------- #
def test_member_sees_members_tier_but_not_officers_or_restricted(client, django_user_model):
    member = _user(django_user_model, "eve:m2", rbac.ROLE_MEMBER)
    visible = _campaign(name="Members Drive", visibility=VIS.MEMBERS)
    officers = _campaign(name="Officers Only", visibility=VIS.OFFICERS)
    restricted = _campaign(name="Restricted Op", visibility=VIS.RESTRICTED)
    client.force_login(member)

    body = client.get(reverse("campaigns:index")).content
    assert b"Members Drive" in body
    assert b"Officers Only" not in body
    assert b"Restricted Op" not in body

    assert client.get(reverse("campaigns:detail", args=[visible.pk])).status_code == 200
    assert client.get(reverse("campaigns:detail", args=[officers.pk])).status_code == 404
    assert client.get(reverse("campaigns:detail", args=[restricted.pk])).status_code == 404


def test_restricted_campaign_visible_to_added_member(client, django_user_model):
    member = _user(django_user_model, "eve:m3", rbac.ROLE_MEMBER)
    restricted = _campaign(name="Inner Circle", visibility=VIS.RESTRICTED)
    restricted.restricted_users.add(member)
    client.force_login(member)
    assert client.get(reverse("campaigns:detail", args=[restricted.pk])).status_code == 200
    assert b"Inner Circle" in client.get(reverse("campaigns:index")).content


# --------------------------------------------------------------------------- #
#  Lifecycle: commander widening, approval gate, illegal transitions
# --------------------------------------------------------------------------- #
def test_commander_member_can_edit_and_start_but_not_approve(client, django_user_model):
    commander = _user(django_user_model, "eve:cmdr", rbac.ROLE_MEMBER)
    approved = _campaign(
        name="Fuel Drive", status=CS.APPROVED, visibility=VIS.OFFICERS,
        commander=commander, target_end_at=_future(),
    )
    client.force_login(commander)

    # Widening rule: a plain-member commander can view + manage their own campaign.
    assert client.get(reverse("campaigns:detail", args=[approved.pk])).status_code == 200
    resp = client.post(reverse("campaigns:edit", args=[approved.pk]), {
        "name": "Fuel Drive v2", "visibility": VIS.OFFICERS, "category": "logistics",
        "progress_mode": "weighted", "commander": str(commander.pk),
    })
    assert resp.status_code == 302
    approved.refresh_from_db()
    assert approved.name == "Fuel Drive v2"

    # Start: approved → active (manage) works for the commander.
    resp = client.post(reverse("campaigns:set_status", args=[approved.pk]), {"to": CS.ACTIVE})
    assert resp.status_code == 302
    approved.refresh_from_db()
    assert approved.status == CS.ACTIVE


def test_commander_cannot_approve_their_own_proposal(client, django_user_model):
    commander = _user(django_user_model, "eve:cmdr2", rbac.ROLE_MEMBER)
    proposed = _campaign(name="Self Approve", status=CS.PROPOSED, commander=commander)
    client.force_login(commander)
    resp = client.post(reverse("campaigns:set_status", args=[proposed.pk]), {"to": CS.APPROVED})
    assert resp.status_code == 403
    proposed.refresh_from_db()
    assert proposed.status == CS.PROPOSED


def test_director_can_approve(client, django_user_model):
    director = _user(django_user_model, "eve:dir", rbac.ROLE_DIRECTOR)
    proposed = _campaign(name="Board Review", status=CS.PROPOSED, visibility=VIS.OFFICERS)
    client.force_login(director)
    resp = client.post(reverse("campaigns:set_status", args=[proposed.pk]), {"to": CS.APPROVED})
    assert resp.status_code == 302
    proposed.refresh_from_db()
    assert proposed.status == CS.APPROVED
    assert AuditLog.objects.filter(
        action="campaigns.approved", target_id=str(proposed.pk)
    ).exists()


def test_illegal_transition_has_no_button_and_is_rejected(client, django_user_model):
    officer = _user(django_user_model, "eve:off1", rbac.ROLE_OFFICER)
    completed = _campaign(name="Done Deal", status=CS.COMPLETED, visibility=VIS.OFFICERS)
    client.force_login(officer)

    body = client.get(reverse("campaigns:detail", args=[completed.pk])).content
    assert b">Start<" not in body  # no active-transition button on a completed campaign

    resp = client.post(reverse("campaigns:set_status", args=[completed.pk]), {"to": CS.ACTIVE})
    assert resp.status_code == 302  # rejected via message, state unchanged
    completed.refresh_from_db()
    assert completed.status == CS.COMPLETED


# --------------------------------------------------------------------------- #
#  Objectives: manual update ownership, verification separation of duties
# --------------------------------------------------------------------------- #
def test_objective_manual_update_owner_ok_stranger_403(client, django_user_model):
    owner = _user(django_user_model, "eve:own", rbac.ROLE_MEMBER)
    stranger = _user(django_user_model, "eve:str", rbac.ROLE_MEMBER)
    campaign = _campaign(name="Metric Run", visibility=VIS.MEMBERS)
    obj = Objective.objects.create(
        campaign=campaign, title="Haul 500 units", owner=owner, status=OS.ACTIVE,
        baseline_value=Decimal(0), target_value=Decimal(500),
    )
    url = reverse("campaigns:objective_update_value", args=[obj.pk])

    client.force_login(owner)
    resp = client.post(url, {"value": "250", "note": "hauled batch"})
    assert resp.status_code == 302
    obj.refresh_from_db()
    assert obj.current_value == Decimal(250)

    client.force_login(stranger)
    assert client.post(url, {"value": "999", "note": "nope"}).status_code == 403
    obj.refresh_from_db()
    assert obj.current_value == Decimal(250)


def test_verify_requires_officer_and_not_self(client, django_user_model):
    claimant = _user(django_user_model, "eve:cla", rbac.ROLE_OFFICER)
    other_officer = _user(django_user_model, "eve:off2", rbac.ROLE_OFFICER)
    member = _user(django_user_model, "eve:mv", rbac.ROLE_MEMBER)
    # Members-tier so the plain member can *view* the campaign — the verify refusal must then be
    # the officer-rank 403, not the invisibility 404.
    campaign = _campaign(name="Verify Me", visibility=VIS.MEMBERS)
    obj = Objective.objects.create(
        campaign=campaign, title="Qualify 10 pilots", status=OS.MET,
        requires_verification=True, target_value=Decimal(10), current_value=Decimal(10),
    )
    # Record who claimed the met value so the separation-of-duties check has a counterpart.
    CampaignActivity.objects.create(
        campaign=campaign, actor=claimant, verb="objective.status",
        target_kind="objective", target_id=obj.pk, after={"status": OS.MET},
    )
    url = reverse("campaigns:objective_verify", args=[obj.pk])

    client.force_login(member)
    assert client.post(url).status_code == 403  # not an officer

    client.force_login(claimant)
    client.post(url)  # officer but the claimant — rejected via message
    obj.refresh_from_db()
    assert obj.verified_by_id is None

    client.force_login(other_officer)
    assert client.post(url).status_code == 302
    obj.refresh_from_db()
    assert obj.verified_by_id == other_officer.pk


# --------------------------------------------------------------------------- #
#  Field-level sensitivity (doc 07 §1.5, T17)
# --------------------------------------------------------------------------- #
def test_budget_panel_hidden_from_plain_officer(client, django_user_model):
    officer = _user(django_user_model, "eve:off3", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:dir2", rbac.ROLE_DIRECTOR)
    campaign = _campaign(name="Big Spend", visibility=VIS.OFFICERS, budget_isk=Decimal(4000000000))
    marker = b"Visible only to directors and the commander."

    client.force_login(officer)
    assert marker not in client.get(reverse("campaigns:detail", args=[campaign.pk])).content

    client.force_login(director)
    assert marker in client.get(reverse("campaigns:detail", args=[campaign.pk])).content


def test_sensitive_objective_value_redacted_in_detail_and_explain(client, django_user_model):
    member = _user(django_user_model, "eve:m4", rbac.ROLE_MEMBER)
    director = _user(django_user_model, "eve:dir3", rbac.ROLE_DIRECTOR)
    campaign = _campaign(name="Reserve Drive", visibility=VIS.MEMBERS)
    obj = Objective.objects.create(
        campaign=campaign, title="SRP reserve", is_sensitive=True,
        target_value=Decimal(5000), current_value=Decimal(4242), weight=3, status=OS.ACTIVE,
    )
    detail = reverse("campaigns:objective_detail", args=[obj.pk])
    explain = reverse("campaigns:explain", args=[campaign.pk])

    client.force_login(member)
    detail_body = client.get(detail).content
    assert b"4242" not in detail_body
    assert b"restricted" in detail_body
    explain_body = client.get(explain).content
    assert b"4242" not in explain_body
    assert b"SRP reserve" in explain_body  # the row (weight/contribution) still shows

    client.force_login(director)
    assert b"4242" in client.get(detail).content


# --------------------------------------------------------------------------- #
#  Nav gating, htmx partial, pagination
# --------------------------------------------------------------------------- #
def test_nav_shows_and_hides_campaigns_by_feature(client, django_user_model):
    member = _user(django_user_model, "eve:m5", rbac.ROLE_MEMBER)
    client.force_login(member)
    on = client.get(reverse("identity:privacy")).content
    assert b'data-acc-key="campaigns"' in on

    set_disabled(["campaigns"])
    off = client.get(reverse("identity:privacy")).content
    assert b'data-acc-key="campaigns"' not in off


def test_htmx_filter_returns_results_partial(client, django_user_model):
    member = _user(django_user_model, "eve:m6", rbac.ROLE_MEMBER)
    _campaign(name="Active Alpha", status=CS.ACTIVE, visibility=VIS.MEMBERS)
    client.force_login(member)
    resp = client.get(reverse("campaigns:index"), {"status": "active"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    assert b'id="cmp-results"' in resp.content
    assert b"data-acc-key" not in resp.content  # the fragment, not the full page + nav


def test_portfolio_pagination(client, django_user_model):
    member = _user(django_user_model, "eve:m7", rbac.ROLE_MEMBER)
    for i in range(30):
        _campaign(name=f"Camp {i:02d}", status=CS.ACTIVE, visibility=VIS.MEMBERS)
    client.force_login(member)
    page1 = client.get(reverse("campaigns:index")).content
    assert b"Page 1 of 2" in page1
    assert b"30 total" in page1
    page2 = client.get(reverse("campaigns:index"), {"page": "2"}).content
    assert b"Page 2 of 2" in page2


def test_manual_create_form_seeds_recognition_from_config(client, django_user_model):
    # A manually created campaign inherits the config recognition defaults, just like a templated
    # one does (#45): the fresh create form pre-selects the configured counts/public defaults.
    from apps.campaigns import config

    officer = _user(django_user_model, "eve:rec_o", rbac.ROLE_OFFICER)
    config.set("recognition", {"default_mode": "counts", "default_public": True})
    client.force_login(officer)
    body = client.get(reverse("campaigns:new")).content.decode()
    assert 'value="counts" selected' in body
    assert 'name="recognition_public" checked' in body


def test_activity_view_paginates(client, django_user_model):
    # The full activity stream is reachable and paged 50 per page (doc 10 line 199, #40).
    member = _user(django_user_model, "eve:act_m", rbac.ROLE_MEMBER)
    c = _campaign()
    CampaignActivity.objects.bulk_create(
        [CampaignActivity(campaign=c, verb=f"note.{i}") for i in range(55)]
    )
    client.force_login(member)
    body = client.get(reverse("campaigns:activity", args=[c.pk])).content
    assert b"Page 1 of 2" in body
    assert client.get(reverse("campaigns:activity", args=[c.pk]), {"page": "2"}).status_code == 200


def test_system_check_clean():
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("check", stdout=out)
    assert "no issues" in out.getvalue()
