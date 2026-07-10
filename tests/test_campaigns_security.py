"""Campaign Command adversarial security suite (design doc 07 Part 3).

A dedicated attacker's-eye complement to the security assertions scattered through
``test_campaigns_views.py`` / ``test_campaigns_services.py`` / ``test_campaigns_collab.py``:
those pin the happy-path controls one at a time; this file probes them *as an attacker* and in
breadth — every object route enumerated for IDOR, every mutating route hammered by someone who
cannot manage, mass-assignment smuggling, field-sensitivity redaction across the surfaces the
per-test files don't reach (report, samples, close-out forms), notification payload restraint on
the lifecycle events, evidence scheme validation, stored-XSS escaping, CSRF, GET-mutation
rejection, and background-job redaction. Cases already proven verbatim elsewhere are not repeated
(cross-checked against the three suites above).

House style: ``pytest.mark.django_db``, ``client.force_login``, role helpers from
``tests/_campaign_utils.py``, HTML-substring assertions, ``monkeypatch`` at service seams.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.campaigns import metrics, notify, services
from apps.campaigns.metrics.base import Measurement, MetricSource
from apps.campaigns.models import (
    Campaign,
    CampaignDependency,
    CampaignEvidence,
    CampaignRecognition,
    DependencyKind,
    EvidenceKind,
    Issue,
    Milestone,
    Risk,
    Workstream,
)
from apps.pingboard.models import Alert

from ._campaign_utils import (
    CS,
    OS,
    _campaign,
    _campaign_lead,
    _director,
    _member,
    _objective,
    _officer,
)

pytestmark = pytest.mark.django_db

VIS = Campaign.Visibility

# Audience kinds that reach a shared surface — a restricted campaign must never use one (doc 07
# §1.5, doc 09 §4.1 rule 1). ``user``/``users`` are the only individually-targeted kinds.
_BROADCAST_KINDS = {"corp", "officer", "director", "channel", "role"}


def _future(days=30):
    return timezone.now() + timezone.timedelta(days=days)


def _campaign_alerts():
    return Alert.objects.filter(source_service="campaigns")


def _full_campaign(dum, *, manager, visibility=VIS.MEMBERS, status=CS.ACTIVE):
    """A campaign carrying one of every child kind, plus a manager-authored evidence row — the
    fixture the IDOR and stranger-mutation matrices both enumerate."""
    c = _campaign(name="Op Fixture", visibility=visibility, status=status,
                  commander=manager, target_end_at=_future())
    ws = Workstream.objects.create(campaign=c, name="Logistics", key="log")
    obj = _objective(c, title="Haul hulls", status=OS.ACTIVE, workstream=ws,
                     baseline_value=Decimal(0), target_value=Decimal(50))
    ms = Milestone.objects.create(campaign=c, title="Staging move", due_at=_future(5))
    risk = Risk.objects.create(campaign=c, description="Cyno risk", severity=4)
    issue = Issue.objects.create(campaign=c, description="Route blocked",
                                 status=Issue.IssueStatus.OPEN)
    dep = CampaignDependency.objects.create(
        campaign=c, from_kind=DependencyKind.OBJECTIVE, from_id=obj.pk,
        to_kind=DependencyKind.EXTERNAL, to_id=0, note="market delivery",
    )
    ev = CampaignEvidence.objects.create(
        campaign=c, attached_kind=EvidenceKind.CAMPAIGN, attached_id=c.pk,
        note="manager evidence", added_by=manager,
    )
    return {"c": c, "ws": ws, "obj": obj, "ms": ms, "risk": risk, "issue": issue,
            "dep": dep, "ev": ev}


# ===========================================================================
#  Anonymous access — every route redirects or 404s, never serves content (doc 07 §1.4)
# ===========================================================================
_ANON_GET_ROUTES = [
    ("campaigns:index", ()),
    ("campaigns:new", ()),
    ("campaigns:template_picker", ()),
    ("campaigns:lessons", ()),
    ("campaigns:workspace", ()),
    ("campaigns:detail", (1,)),
    ("campaigns:edit", (1,)),
    ("campaigns:explain", (1,)),
    ("campaigns:timeline", (1,)),
    ("campaigns:close", (1,)),
    ("campaigns:report", (1,)),
    ("campaigns:recognition", (1,)),
    ("campaigns:objective_detail", (1,)),
    ("campaigns:objective_edit", (1,)),
    ("campaigns:milestone_edit", (1,)),
    ("campaigns:workstream_edit", (1,)),
    ("campaigns:risk_edit", (1,)),
]


@pytest.mark.parametrize("name,args", _ANON_GET_ROUTES)
def test_anonymous_gets_no_content_on_any_route(client, name, args):
    resp = client.get(reverse(name, args=args))
    # Never a rendered page: a login redirect (302 to the login URL) or 404, nothing else.
    assert resp.status_code in (302, 404)
    if resp.status_code == 302:
        assert "/login" in resp["Location"] or "next=" in resp["Location"]


def test_anonymous_post_mutation_is_blocked(client, django_user_model):
    director = _director(django_user_model, "anon_owner")
    c = _campaign(status=CS.PROPOSED, commander=director)
    resp = client.post(reverse("campaigns:set_status", args=[c.pk]), {"to": CS.APPROVED})
    assert resp.status_code in (302, 404)  # login redirect / 404, never executed
    c.refresh_from_db()
    assert c.status == CS.PROPOSED


# ===========================================================================
#  IDOR / BOLA — a member enumerating an invisible campaign's pks (T1)
# ===========================================================================
def test_direct_url_access_denied_across_all_subresources(client, django_user_model):
    """A plain member probes every object + subresource URL of an officers-tier campaign by pk.
    Each returns 404 (the no-existence-oracle rule) — never 403, never content."""
    member = _member(django_user_model, "idor_probe")
    officer = _officer(django_user_model, "idor_owner")
    kit = _full_campaign(django_user_model, manager=officer, visibility=VIS.OFFICERS)
    c, obj, ms, ws, risk = kit["c"], kit["obj"], kit["ms"], kit["ws"], kit["risk"]

    client.force_login(member)
    probes = [
        reverse("campaigns:detail", args=[c.pk]),
        reverse("campaigns:edit", args=[c.pk]),
        reverse("campaigns:explain", args=[c.pk]),
        reverse("campaigns:timeline", args=[c.pk]),
        reverse("campaigns:report", args=[c.pk]),
        reverse("campaigns:recognition", args=[c.pk]),
        reverse("campaigns:close", args=[c.pk]),
        reverse("campaigns:objective_create", args=[c.pk]),
        reverse("campaigns:objective_detail", args=[obj.pk]),
        reverse("campaigns:objective_edit", args=[obj.pk]),
        reverse("campaigns:milestone_edit", args=[ms.pk]),
        reverse("campaigns:workstream_edit", args=[ws.pk]),
        reverse("campaigns:risk_edit", args=[risk.pk]),
    ]
    for url in probes:
        assert client.get(url).status_code == 404, url
    # The campaign name must not leak through any surface, including the portfolio list.
    assert b"Op Fixture" not in client.get(reverse("campaigns:index")).content


def test_idor_mutation_on_invisible_campaign_is_404_not_403(client, django_user_model):
    """Writes to an invisible campaign 404 (no oracle) — an attacker cannot distinguish
    'forbidden' from 'nonexistent'."""
    member = _member(django_user_model, "idor_write")
    officer = _officer(django_user_model, "idor_write_owner")
    kit = _full_campaign(django_user_model, manager=officer, visibility=VIS.DIRECTORS)
    client.force_login(member)
    assert client.post(reverse("campaigns:set_status", args=[kit["c"].pk]),
                       {"to": CS.APPROVED}).status_code == 404
    assert client.post(reverse("campaigns:objective_update_value", args=[kit["obj"].pk]),
                       {"value": "9", "note": "x"}).status_code == 404
    kit["obj"].refresh_from_db()
    assert kit["obj"].current_value is None


def test_manage_capability_never_pierces_visibility(client, django_user_model):
    """A ``campaign_lead`` holds ``campaign.manage`` but rank 0: an officers-tier campaign they are
    not attached to stays invisible (doc 07 §1.3 footnote 4) — capability is checked only after
    visibility, so it can never reach a campaign the tier hides."""
    lead = _campaign_lead(django_user_model, "cap_lead")
    officer = _officer(django_user_model, "cap_owner")
    kit = _full_campaign(django_user_model, manager=officer, visibility=VIS.OFFICERS)
    client.force_login(lead)
    assert client.get(reverse("campaigns:detail", args=[kit["c"].pk])).status_code == 404
    assert client.post(reverse("campaigns:edit", args=[kit["c"].pk]),
                       {"name": "hijack", "visibility": VIS.OFFICERS}).status_code == 404


# ===========================================================================
#  Stranger POST to every manage-gated mutation on a VISIBLE campaign → 403 (T1)
# ===========================================================================
def test_stranger_post_to_every_mutation_forbidden(client, django_user_model):
    """A plain member can *see* a members-tier campaign but cannot manage it: every mutating route
    that requires manage/owner/officer authority returns 403 (a visible-but-forbidden action),
    and nothing changes. This is the 403-vs-404 other half of the matrix."""
    member = _member(django_user_model, "stranger")
    officer = _officer(django_user_model, "stranger_owner")
    kit = _full_campaign(django_user_model, manager=officer, visibility=VIS.MEMBERS)
    c, obj, ms, ws, risk, issue, dep, ev = (
        kit["c"], kit["obj"], kit["ms"], kit["ws"], kit["risk"],
        kit["issue"], kit["dep"], kit["ev"],
    )
    client.force_login(member)

    mutations = [
        (reverse("campaigns:edit", args=[c.pk]), {"name": "x", "visibility": VIS.MEMBERS}),
        (reverse("campaigns:set_status", args=[c.pk]), {"to": CS.PAUSED, "reason": "x"}),
        (reverse("campaigns:set_status", args=[c.pk]), {"to": CS.APPROVED}),
        (reverse("campaigns:objective_create", args=[c.pk]), {"title": "x"}),
        (reverse("campaigns:objective_edit", args=[obj.pk]), {"title": "x"}),
        (reverse("campaigns:objective_update_value", args=[obj.pk]), {"value": "5", "note": "x"}),
        (reverse("campaigns:objective_verify", args=[obj.pk]), {}),
        (reverse("campaigns:objective_set_status", args=[obj.pk]), {"to": OS.MET}),
        (reverse("campaigns:objective_task", args=[obj.pk]), {"title": "x"}),
        (reverse("campaigns:milestone_create", args=[c.pk]), {"title": "x"}),
        (reverse("campaigns:milestone_edit", args=[ms.pk]), {"title": "x"}),
        (reverse("campaigns:milestone_set_status", args=[ms.pk]), {"to": "done"}),
        (reverse("campaigns:workstream_create", args=[c.pk]), {"name": "x"}),
        (reverse("campaigns:workstream_edit", args=[ws.pk]), {"name": "x"}),
        (reverse("campaigns:risk_create", args=[c.pk]),
         {"description": "x", "probability": "medium", "impact": "medium"}),
        (reverse("campaigns:risk_edit", args=[risk.pk]), {"description": "x"}),
        (reverse("campaigns:dependency_create", args=[c.pk]),
         {"from_kind": "objective", "from_id": str(obj.pk), "to_kind": "external", "note": "x"}),
        (reverse("campaigns:dependency_resolve", args=[dep.pk]), {}),
        (reverse("campaigns:issue_resolve", args=[issue.pk]), {"resolution_notes": "x"}),
        (reverse("campaigns:issue_escalate", args=[issue.pk]), {"reason": "x"}),
        (reverse("campaigns:evidence_create", args=[c.pk]),
         {"attached_kind": "campaign", "url": "https://ok.example", "note": "x"}),
        (reverse("campaigns:evidence_delete", args=[ev.pk]), {}),
        (reverse("campaigns:recognition", args=[c.pk]),
         {"user": str(member.pk), "category": "x", "reason": "x"}),
        (reverse("campaigns:close", args=[c.pk]), {"final_status": "completed"}),
        (reverse("campaigns:save_template", args=[c.pk]), {"template_name": "x"}),
    ]
    for url, data in mutations:
        assert client.post(url, data).status_code == 403, url

    # Nothing leaked into state: no new objectives/evidence/recognition, status unchanged.
    c.refresh_from_db()
    assert c.status == CS.ACTIVE
    assert c.objectives.count() == 1
    assert CampaignEvidence.objects.filter(campaign=c).count() == 1
    assert CampaignRecognition.objects.filter(campaign=c).count() == 0


def test_non_director_cannot_approve(client, django_user_model):
    """Approval is director-only regardless of manage capability: commander, campaign_lead and a
    plain officer all 403; the director succeeds and it is audited (T3)."""
    commander = _member(django_user_model, "appr_cmd")
    lead = _campaign_lead(django_user_model, "appr_lead")
    officer = _officer(django_user_model, "appr_off")
    director = _director(django_user_model, "appr_dir")
    c = _campaign(name="Board", status=CS.PROPOSED, visibility=VIS.OFFICERS, commander=commander)

    for actor in (commander, lead, officer):
        client.force_login(actor)
        # A visible campaign forbids the action → 403 (commander/officer see it; the lead is the
        # commander's campaign only if attached — here officers-tier so lead 404s, still not approved).
        assert client.post(reverse("campaigns:set_status", args=[c.pk]),
                           {"to": CS.APPROVED}).status_code in (403, 404)
        c.refresh_from_db()
        assert c.status == CS.PROPOSED

    client.force_login(director)
    assert client.post(reverse("campaigns:set_status", args=[c.pk]),
                       {"to": CS.APPROVED}).status_code == 302
    c.refresh_from_db()
    assert c.status == CS.APPROVED
    assert AuditLog.objects.filter(action="campaigns.approved", target_id=str(c.pk)).exists()


# ===========================================================================
#  Mass assignment (T4)
# ===========================================================================
def test_mass_assignment_campaign_edit_ignores_privileged_fields(client, django_user_model):
    """An officer manager smuggles lifecycle/derived/budget columns into the edit form; the
    allow-list handler reads none of them (doc 07 T4). Budget is dropped because a plain officer
    is neither director nor commander."""
    officer = _officer(django_user_model, "mass_off")
    c = _campaign(name="Def", status=CS.APPROVED, visibility=VIS.OFFICERS, target_end_at=_future())
    client.force_login(officer)
    resp = client.post(reverse("campaigns:edit", args=[c.pk]), {
        "name": "Def v2", "visibility": VIS.OFFICERS, "category": "deployment",
        "progress_mode": "weighted",
        # Smuggled — none of these are in the allow-list:
        "status": CS.ACTIVE, "health": "critical", "progress_pct": "99",
        "spent_isk": "88888", "budget_isk": "77777", "is_sensitive": "1",
    })
    assert resp.status_code == 302
    c.refresh_from_db()
    assert c.name == "Def v2"          # legit field applied
    assert c.status == CS.APPROVED     # lifecycle unchanged
    assert c.health == Campaign.Health.UNKNOWN
    assert c.progress_pct == 0
    assert c.spent_isk == Decimal(0)
    assert c.budget_isk is None        # budget key dropped for a non-director non-commander


def test_mass_assignment_objective_edit_ignores_status_and_value(client, django_user_model):
    """The objective edit form cannot set status, current_value or verified_by — those move only
    through their dedicated services (doc 07 T4)."""
    officer = _officer(django_user_model, "mass_obj_off")
    c = _campaign(name="Metrics", status=CS.ACTIVE, visibility=VIS.OFFICERS)
    obj = _objective(c, title="Reserve", status=OS.ACTIVE, current_value=Decimal(10),
                     target_value=Decimal(100))
    client.force_login(officer)
    resp = client.post(reverse("campaigns:objective_edit", args=[obj.pk]), {
        "title": "Reserve v2", "direction": "gte", "weight": "1",
        # Smuggled:
        "status": OS.MET, "current_value": "999", "verified_by": str(officer.pk),
        "progress_pct": "100",
    })
    assert resp.status_code == 302
    obj.refresh_from_db()
    assert obj.title == "Reserve v2"
    assert obj.status == OS.ACTIVE
    assert obj.current_value == Decimal(10)
    assert obj.verified_by_id is None


def test_visibility_change_by_manager_is_audited(client, django_user_model):
    """A manager legitimately changing visibility writes the ``campaigns.visibility_changed`` audit
    row (T4) — the sensitive verb is always recorded."""
    officer = _officer(django_user_model, "vis_off")
    c = _campaign(name="Shift", status=CS.APPROVED, visibility=VIS.OFFICERS, target_end_at=_future())
    client.force_login(officer)
    client.post(reverse("campaigns:edit", args=[c.pk]),
                {"name": "Shift", "visibility": VIS.MEMBERS, "category": "deployment",
                 "progress_mode": "weighted"})
    c.refresh_from_db()
    assert c.visibility == VIS.MEMBERS
    assert AuditLog.objects.filter(
        action="campaigns.visibility_changed", target_id=str(c.pk)
    ).exists()


# ===========================================================================
#  Field sensitivity — redaction on the surfaces the view suite does not reach (T17)
# ===========================================================================
def test_sensitive_objective_value_redacted_in_report_and_samples(client, django_user_model):
    """The close-out report and its objective rows must not carry a sensitive value (or its stored
    samples) for a non-privileged viewer; a director sees it. Complements the detail/explain
    redaction already covered in the view suite."""
    member = _member(django_user_model, "rep_member")
    director = _director(django_user_model, "rep_dir")
    c = _campaign(name="Reserve Run", status=CS.COMPLETED, visibility=VIS.MEMBERS,
                  actual_end_at=timezone.now())
    obj = _objective(c, title="SRP reserve", is_sensitive=True, status=OS.MET,
                     target_value=Decimal(5000), current_value=Decimal(4242))
    obj.samples.create(value=Decimal(4242), measured_at=timezone.now())
    url = reverse("campaigns:report", args=[c.pk])

    client.force_login(member)
    body = client.get(url).content
    assert b"4242" not in body
    assert b"restricted" in body
    assert b"SRP reserve" in body  # the row still shows (weight/status), only the value is masked

    client.force_login(director)
    assert b"4242" in client.get(url).content


def test_budget_hidden_from_officer_in_report_and_edit_form(client, django_user_model):
    """Budget figures are director + commander only across the report and the edit form — a plain
    officer sees neither the variance panel nor the budget input (T17)."""
    officer = _officer(django_user_model, "bud_off")
    director = _director(django_user_model, "bud_dir")
    c = _campaign(name="Big Spend", status=CS.COMPLETED, visibility=VIS.OFFICERS,
                  actual_end_at=timezone.now(), budget_isk=Decimal(4000000000),
                  spent_isk=Decimal(3500000000))
    report = reverse("campaigns:report", args=[c.pk])

    client.force_login(officer)
    assert b"Budget variance" not in client.get(report).content
    # Edit form omits the budget input for the officer.
    assert b'name="budget_isk"' not in client.get(reverse("campaigns:edit", args=[c.pk])).content

    client.force_login(director)
    assert b"Budget variance" in client.get(report).content
    assert b'name="budget_isk"' in client.get(reverse("campaigns:edit", args=[c.pk])).content


def test_beat_measured_sensitive_value_stays_redacted(client, django_user_model, monkeypatch):
    """A value written by the refresh beat (no user context) is still stripped on read for an
    officer who is neither director nor commander (T12): jobs write, they do not disclose."""

    class _SensitiveSource(MetricSource):
        key = "test.sensitive"
        label = "Test sensitive"
        unit = "isk"
        data_class = "default"
        params_schema: list = []

        def measure(self, params):
            return Measurement(value=Decimal(4242), as_of=timezone.now(), detail={})

    src = _SensitiveSource()
    metrics.register(src)
    try:
        officer = _officer(django_user_model, "beat_off")
        director = _director(django_user_model, "beat_dir")
        c = _campaign(name="Wallet Watch", status=CS.ACTIVE, visibility=VIS.MEMBERS)
        obj = _objective(c, title="Wallet", metric_source="test.sensitive", is_sensitive=True,
                         status=OS.ACTIVE, baseline_value=Decimal(0), target_value=Decimal(10000))
        services.run_metric_refresh()  # the beat measures + writes the value
        obj.refresh_from_db()
        assert obj.current_value == Decimal(4242)  # persisted...

        url = reverse("campaigns:objective_detail", args=[obj.pk])
        client.force_login(officer)
        body = client.get(url).content
        assert b"4242" not in body          # ...but never rendered to a non-privileged viewer
        assert b"restricted" in body
        client.force_login(director)
        assert b"4242" in client.get(url).content
    finally:
        metrics.unregister("test.sensitive")


# ===========================================================================
#  Notification payload restraint on lifecycle events (T2/T11)
# ===========================================================================
def test_restricted_lifecycle_alerts_are_targeted_and_name_only(
    client, django_user_model, django_capture_on_commit_callbacks
):
    """Starting a restricted campaign fans out to individually-targeted leadership only — never a
    broadcast audience — and the alert body carries the campaign name, no rationale/summary or
    objective detail (doc 07 §1.5, doc 09 §4.1). Every campaign alert emitted is name-only +
    targeted."""
    director = _director(django_user_model, "rn_dir", cid=4101)  # corp member → resolvable
    c = _campaign(
        name="Nightfall", status=CS.APPROVED, visibility=VIS.RESTRICTED, commander=director,
        rationale="covert K-6 forward staging plan", summary="secret staging detail",
        target_end_at=_future(),
    )
    _objective(c, title="Prime cynos", status=OS.ACTIVE)
    c.restricted_users.add(director)

    with django_capture_on_commit_callbacks(execute=True):
        services.set_status(c, CS.ACTIVE, director)

    started = _campaign_alerts().filter(idempotency_key=f"campaigns:status:{c.pk}:active").first()
    assert started is not None
    assert started.audience.get("kind") == "users"           # individually targeted, not "corp"
    assert "Nightfall" in started.body
    for leak in (b"covert", b"staging", b"cyno", b"K-6"):
        assert leak not in started.body.lower().encode()

    # No restricted-campaign alert ever used a broadcast audience.
    for alert in _campaign_alerts():
        assert alert.audience.get("kind") not in _BROADCAST_KINDS, alert.idempotency_key


def test_restricted_approval_request_is_directors_only(django_user_model):
    """The ``proposed`` approval request for a restricted campaign is targeted at directors as a
    ``users`` list — never the broadcast ``director`` role kind that a non-restricted campaign uses
    (doc 09 §4.1 rule 1)."""
    director = _director(django_user_model, "ra_dir", cid=4201)
    c = _campaign(name="Hushed", status=CS.PROPOSED, visibility=VIS.RESTRICTED, commander=director)
    c.restricted_users.add(director)
    # A proposed transition already happened conceptually; call the notifier directly (its own
    # chokepoint) and assert the audience shape.
    notify.approval_needed(c)
    alert = _campaign_alerts().filter(idempotency_key__startswith=f"campaigns:approval:{c.pk}:").first()
    assert alert is not None
    assert alert.audience.get("kind") == "users"
    assert alert.audience.get("kind") not in _BROADCAST_KINDS


# ===========================================================================
#  Evidence link validation & rendering (T8)
# ===========================================================================
@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "http://insecure.example/phish",
    "ftp://evil.example/x",
    "//evil.example",
])
def test_evidence_url_scheme_whitelist_rejects_dangerous(client, django_user_model, bad_url):
    """Only ``https://`` evidence URLs are accepted; ``javascript:``/``data:``/``http:`` and other
    schemes are refused at the service boundary — no row is created (doc 07 T8)."""
    officer = _officer(django_user_model, "ev_scheme")
    c = _campaign(name="Ev", status=CS.ACTIVE, visibility=VIS.OFFICERS, commander=officer)
    client.force_login(officer)
    client.post(reverse("campaigns:evidence_create", args=[c.pk]),
                {"attached_kind": "campaign", "url": bad_url, "note": ""})
    assert CampaignEvidence.objects.filter(campaign=c).count() == 0


def test_evidence_rendered_with_noopener_nofollow(client, django_user_model):
    """A stored https evidence link renders as an anchor carrying ``rel="noopener ... nofollow"``,
    never embedded/executed (doc 07 T8)."""
    officer = _officer(django_user_model, "ev_render")
    c = _campaign(name="Ev2", status=CS.ACTIVE, visibility=VIS.OFFICERS, commander=officer)
    CampaignEvidence.objects.create(
        campaign=c, attached_kind=EvidenceKind.CAMPAIGN, attached_id=c.pk,
        url="https://ref.example/proof", added_by=officer,
    )
    client.force_login(officer)
    body = client.get(reverse("campaigns:detail", args=[c.pk])).content.decode()
    assert 'href="https://ref.example/proof"' in body
    # The whole opening <a> tag carries the rel attributes (link rendered, never embedded).
    start = body.index('href="https://ref.example/proof"')
    anchor = body[start:body.index(">", start)]
    assert 'rel="noopener noreferrer nofollow"' in anchor


# ===========================================================================
#  Stored XSS — user text is autoescaped everywhere it renders (T5)
# ===========================================================================
def test_stored_xss_escaped_in_campaign_evidence_and_lessons(client, django_user_model):
    """A ``<script>`` payload stored in the campaign name/description, an evidence note, and the
    lessons/outcome prose renders escaped — the raw tag never reaches the response (doc 07 T5)."""
    payload = "<script>alert('pwn')</script>"
    director = _director(django_user_model, "xss_dir")
    c = _campaign(
        name=f"Camp {payload}", status=CS.COMPLETED, visibility=VIS.MEMBERS,
        actual_end_at=timezone.now(), description=payload,
        outcome_summary=payload, lessons_learned=payload,
    )
    CampaignEvidence.objects.create(
        campaign=c, attached_kind=EvidenceKind.CAMPAIGN, attached_id=c.pk,
        note=payload, added_by=director,
    )
    client.force_login(director)
    for url in (reverse("campaigns:detail", args=[c.pk]),
                reverse("campaigns:report", args=[c.pk])):
        body = client.get(url).content
        assert b"<script>alert('pwn')</script>" not in body
        assert b"&lt;script&gt;" in body  # escaped form is present


# ===========================================================================
#  Recognition separation of duties (T3/T19)
# ===========================================================================
def test_recognition_self_award_blocked_and_reason_required(client, django_user_model):
    """A commander cannot record recognition for their own account (self-award block) and an empty
    reason is rejected; a director awarding another pilot succeeds and is audited (T19)."""
    commander = _member(django_user_model, "rec_cmd")
    director = _director(django_user_model, "rec_dir")
    pilot = _member(django_user_model, "rec_pilot")
    c = _campaign(name="Recog", status=CS.ACTIVE, visibility=VIS.MEMBERS, commander=commander,
                  recognition_mode=Campaign.RecognitionMode.COUNTS)
    url = reverse("campaigns:recognition", args=[c.pk])

    # Commander self-award — blocked by the service, no row written.
    client.force_login(commander)
    client.post(url, {"user": str(commander.pk), "category": "logi", "reason": "me"})
    assert not CampaignRecognition.objects.filter(campaign=c, user=commander).exists()

    # Director, empty reason — rejected.
    client.force_login(director)
    client.post(url, {"user": str(pilot.pk), "category": "logi", "reason": ""})
    assert not CampaignRecognition.objects.filter(campaign=c, user=pilot).exists()

    # Director awards the pilot with a reason — recorded + audited.
    client.post(url, {"user": str(pilot.pk), "category": "logi", "reason": "hauled the fleet"})
    assert CampaignRecognition.objects.filter(campaign=c, user=pilot).exists()
    assert AuditLog.objects.filter(
        action="campaigns.recognition_adjusted", target_id=str(c.pk)
    ).exists()


# ===========================================================================
#  CSRF & GET-mutation rejection (T6)
# ===========================================================================
def test_post_without_csrf_token_is_rejected(django_user_model):
    """An enforce-CSRF client POST without a token is rejected (403) — the global
    ``CsrfViewMiddleware`` protects every mutation (doc 07 T6)."""
    csrf_client = Client(enforce_csrf_checks=True)
    officer = _officer(django_user_model, "csrf_off")
    c = _campaign(name="Csrf", status=CS.APPROVED, visibility=VIS.OFFICERS,
                  commander=officer, target_end_at=_future())
    csrf_client.force_login(officer)
    resp = csrf_client.post(reverse("campaigns:set_status", args=[c.pk]), {"to": CS.ACTIVE})
    assert resp.status_code == 403
    c.refresh_from_db()
    assert c.status == CS.APPROVED


@pytest.mark.parametrize("name,make_args", [
    ("campaigns:set_status", "campaign"),
    ("campaigns:objective_update_value", "objective"),
    ("campaigns:objective_verify", "objective"),
    ("campaigns:objective_set_status", "objective"),
    ("campaigns:evidence_create", "campaign"),
    ("campaigns:dependency_create", "campaign"),
    ("campaigns:save_template", "campaign"),
])
def test_mutation_routes_reject_get(client, django_user_model, name, make_args):
    """Every mutation route is POST-only: a GET returns 405 with no side effect (doc 07 T6). This
    also keeps impersonated read-only sessions safe for free."""
    officer = _officer(django_user_model, "get_off")
    c = _campaign(name="GetOnly", status=CS.ACTIVE, visibility=VIS.OFFICERS, commander=officer)
    obj = _objective(c, title="o", status=OS.ACTIVE)
    args = [c.pk] if make_args == "campaign" else [obj.pk]
    client.force_login(officer)
    assert client.get(reverse(name, args=args)).status_code == 405


# ===========================================================================
#  Injection-safe portfolio filters (T7) + dependency DoS guard via the view (T20)
# ===========================================================================
def test_portfolio_filters_are_injection_safe(client, django_user_model):
    """Hostile filter strings map to whitelisted ORM lookups and never reach raw SQL: the portfolio
    returns 200 with a valid (empty) result set, never a 500 (doc 07 T7)."""
    member = _member(django_user_model, "sqli")
    _campaign(name="Real", status=CS.ACTIVE, visibility=VIS.MEMBERS)
    client.force_login(member)
    hostile = {
        "status": "' OR 1=1--",
        "health": "'; DROP TABLE campaigns_campaign;--",
        "category": "<script>",
        "commander": "1 OR 1=1",
        "tag": "' UNION SELECT 1--",
        "q": "%' OR '1'='1",
    }
    resp = client.get(reverse("campaigns:index"), hostile)
    assert resp.status_code == 200


def test_dependency_cycle_rejected_via_view(client, django_user_model):
    """The dependency-create route rejects a 2-cycle (A→B then B→A) with a message and creates no
    second edge — the acyclicity DoS guard holds at the HTTP boundary (doc 07 T20)."""
    officer = _officer(django_user_model, "cycle_off")
    c = _campaign(name="Graph", status=CS.ACTIVE, visibility=VIS.OFFICERS, commander=officer)
    a = _objective(c, title="A", status=OS.ACTIVE)
    b = _objective(c, title="B", status=OS.ACTIVE)
    client.force_login(officer)
    url = reverse("campaigns:dependency_create", args=[c.pk])
    client.post(url, {"from_kind": "objective", "from_id": str(a.pk),
                      "to_kind": "objective", "to_id": str(b.pk)})
    client.post(url, {"from_kind": "objective", "from_id": str(b.pk),
                      "to_kind": "objective", "to_id": str(a.pk)})  # would close a cycle
    assert CampaignDependency.objects.filter(campaign=c).count() == 1
