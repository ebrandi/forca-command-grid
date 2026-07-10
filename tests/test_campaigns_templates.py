"""Campaign Command template tests (doc 04 §13, doc 12 §4.1 Templates).

Covers the builtin seed (idempotency + reverse deleting only builtins), template instantiation
(day-offsets materialised from the start date; the no-users / no-dates / no-instance-ids blueprint
invariants; the reference campaign's 12 objectives + 9 workstreams), save-as-template stripping,
and the create-from-template picker flow through the client.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.campaigns import services
from apps.campaigns.models import Campaign, CampaignTemplate
from apps.campaigns.templates_builtin import BUILTIN_KEYS, seed_builtin_templates

from ._campaign_utils import _campaign, _director, _objective, _officer, _reference_campaign

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
#  Seed migration semantics
# --------------------------------------------------------------------------- #
def test_builtins_seeded_by_migration():
    """The data migration seeded every builtin blueprint as a builtin row."""
    for key in BUILTIN_KEYS:
        tpl = CampaignTemplate.objects.get(key=key)
        assert tpl.is_builtin is True
        assert tpl.blueprint  # non-empty structure


def test_seed_is_idempotent():
    before = CampaignTemplate.objects.count()
    n = seed_builtin_templates()
    assert n == len(BUILTIN_KEYS)
    assert CampaignTemplate.objects.count() == before  # upsert, never duplicate
    # The reference campaign keeps its 12/9 shape after a re-seed.
    tpl = CampaignTemplate.objects.get(key="armour_bs_deployment")
    assert len(tpl.blueprint["objectives"]) == 12
    assert len(tpl.blueprint["workstreams"]) == 9


def test_reseed_never_clobbers_operator_edits(django_user_model):
    # get_or_create seeding must never reactivate or revert an operator-edited builtin (doc 06 §9, #43).
    tpl = CampaignTemplate.objects.get(key="armour_bs_deployment")
    tpl.active = False
    tpl.name = "Operator-renamed"
    tpl.save(update_fields=["active", "name"])

    seed_builtin_templates()

    tpl.refresh_from_db()
    assert tpl.active is False              # stays deactivated
    assert tpl.name == "Operator-renamed"  # edit preserved


def test_reverse_deletes_only_builtins(django_user_model):
    """The reverse step removes builtin keys and leaves custom save-as-template rows intact."""
    custom = CampaignTemplate.objects.create(
        key="my_custom", name="Custom", blueprint={}, is_builtin=False
    )
    # Mirror the migration's unseed predicate.
    CampaignTemplate.objects.filter(key__in=BUILTIN_KEYS, is_builtin=True).delete()
    assert not CampaignTemplate.objects.filter(key__in=BUILTIN_KEYS).exists()
    assert CampaignTemplate.objects.filter(pk=custom.pk).exists()


# --------------------------------------------------------------------------- #
#  Instantiation
# --------------------------------------------------------------------------- #
def test_reference_campaign_shape(django_user_model):
    director = _director(django_user_model)
    campaign = _reference_campaign(director)
    assert campaign.status == Campaign.Status.DRAFT
    assert campaign.objectives.count() == 12
    assert campaign.workstreams.count() == 9
    assert campaign.milestones.count() >= 1
    assert campaign.risks.count() >= 1
    # Creator becomes commander by default; conservative officers visibility (doc 04 §13 D18).
    assert campaign.commander_id == director.pk
    assert campaign.visibility == Campaign.Visibility.OFFICERS


def test_instantiate_materialises_offsets(django_user_model):
    director = _director(django_user_model)
    start = timezone.now()
    campaign = _reference_campaign(director, start_at=start)
    # A blueprint due-offset became an absolute due date measured from the start.
    obj = campaign.objectives.filter(metric_source="srp.reserve").first()
    assert obj is not None and obj.due_at is not None
    assert obj.due_at > start


def test_instantiate_invariants_no_users_no_ids(django_user_model):
    """The reference campaign carries no owners, no instance metric ids, and only structure."""
    director = _director(django_user_model)
    campaign = _reference_campaign(director)
    for obj in campaign.objectives.all():
        assert obj.owner_id is None  # roles are unassigned placeholders
        # instance-bound ids are never baked into a blueprint's params
        assert "doctrine_id" not in obj.metric_params
        assert "stockpile_id" not in obj.metric_params
        assert "op_type" not in obj.metric_params
        assert obj.current_value is None  # no measured values
    # The SRP objective defaulted sensitive on (from the blueprint).
    srp = campaign.objectives.get(metric_source="srp.reserve")
    assert srp.is_sensitive is True


def test_instantiate_without_dates_leaves_due_at_null(django_user_model):
    director = _director(django_user_model)
    template = CampaignTemplate.objects.get(key="armour_bs_deployment")
    campaign = services.instantiate_template(template, director, start_at=None, target_end_at=None)
    assert campaign.start_at is None
    assert all(o.due_at is None for o in campaign.objectives.all())
    assert all(m.due_at is None for m in campaign.milestones.all())


# --------------------------------------------------------------------------- #
#  Save as template
# --------------------------------------------------------------------------- #
def test_save_as_template_strips_instance_data(django_user_model):
    director = _director(django_user_model)
    start = timezone.now()
    campaign = _campaign(
        name="Live one", commander=director, start_at=start,
        target_end_at=start + timezone.timedelta(days=10),
    )
    ws = campaign.workstreams.create(name="Doctrine", key="doctrine")
    _objective(
        campaign, title="Qualify pilots", owner=director, workstream=ws,
        metric_source="doctrine.qualified_pilots",
        metric_params={"doctrine_id": 999, "active_days": 30},
        current_value=Decimal("7"), target_value=Decimal("35"),
        due_at=start + timezone.timedelta(days=8),
    )
    tpl = services.save_as_template(campaign, director, key="my-run", name="My Run", description="d")

    assert tpl.is_builtin is False and tpl.created_from_id == campaign.pk
    bp = tpl.blueprint
    obj = bp["objectives"][0]
    assert "owner" not in obj  # people stripped
    assert obj["metric_params"] == {"active_days": 30}  # instance id stripped, knob kept
    assert obj["due_offset_days"] == 8  # absolute date became a day-offset
    assert "current_value" not in obj  # measured value not copied
    assert Decimal(obj["target_value"]) == Decimal("35")  # suggested default retained

    # Round-trips: instantiating the saved template rebuilds the structure.
    rebuilt = services.instantiate_template(tpl, director, start_at=start)
    assert rebuilt.objectives.count() == 1
    assert rebuilt.workstreams.count() == 1


def test_save_as_template_rejects_duplicate_key(django_user_model):
    director = _director(django_user_model)
    campaign = _campaign(commander=director)
    with pytest.raises(ValidationError):
        services.save_as_template(campaign, director, key="armour_bs_deployment",
                                  name="clash", description="")


# --------------------------------------------------------------------------- #
#  Picker + create-from-template flow (client)
# --------------------------------------------------------------------------- #
def test_template_picker_lists_builtins(client, django_user_model):
    officer = _officer(django_user_model)
    client.force_login(officer)
    resp = client.get(reverse("campaigns:template_picker"))
    assert resp.status_code == 200
    assert b"Establish Armour Battleship Deployment Readiness" in resp.content


def test_create_from_template_prefill_and_post(client, django_user_model):
    officer = _officer(django_user_model)
    client.force_login(officer)
    # GET prefills the form from the blueprint.
    resp = client.get(reverse("campaigns:new") + "?template=armour_bs_deployment")
    assert resp.status_code == 200
    assert b"From this template" in resp.content

    # POST instantiates the whole structure as one draft.
    start = timezone.now()
    resp = client.post(reverse("campaigns:new"), {
        "template_key": "armour_bs_deployment",
        "name": "Armour BS — East",
        "start_at": start.strftime("%Y-%m-%dT%H:%M"),
        "target_end_at": (start + timezone.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),
    })
    assert resp.status_code == 302
    campaign = Campaign.objects.get(name="Armour BS — East")
    assert campaign.objectives.count() == 12
    assert campaign.workstreams.count() == 9
    assert campaign.created_by_id == officer.pk


def test_non_manager_cannot_open_picker(client, django_user_model):
    from ._campaign_utils import _member

    member = _member(django_user_model)
    client.force_login(member)
    resp = client.get(reverse("campaigns:template_picker"))
    assert resp.status_code == 403
