"""Regression tests for issues found in the Phase 4-7 adversarial QA round."""
from __future__ import annotations

import pytest
import responses

from apps.market.models import MarketLocation, MarketPrice
from apps.market.services import update_price_from_orders
from apps.recommendations import engine, notify
from apps.recommendations.models import Recommendation
from apps.stockpile.models import HaulingTask, Stockpile
from apps.stockpile.services import generate_hauling_tasks, record_manual_stock


# --- act() IDOR: deny-by-default -------------------------------------------
@pytest.mark.django_db
def test_member_cannot_act_on_unowned_non_character_rec(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)

    # A member-permission rec whose subject is NOT a character the user owns.
    rec = Recommendation.objects.create(
        type=Recommendation.Type.STOCK_SHORTAGE,
        subject_type="type",
        subject_id="587",
        message="x",
        required_permission="member",
    )
    resp = client.post(f"/recommendations/{rec.pk}/act/", {"action": "dismiss"})
    assert resp.status_code == 403  # deny-by-default else branch
    rec.refresh_from_db()
    assert rec.state == Recommendation.State.NEW


# --- doctrine readiness with no members emits nothing ----------------------
@pytest.mark.django_db
def test_doctrine_readiness_no_members_no_draft(sde):
    from apps.doctrines.models import Doctrine, DoctrineCategory

    Doctrine.objects.create(name="D", category=DoctrineCategory.objects.create(key="c", label="C"))
    assert engine.eval_doctrine_readiness() == []  # no corp members -> no noise


# --- hauling not duplicated once claimed -----------------------------------
@pytest.mark.django_db
def test_hauling_not_duplicated_after_claim(sde):
    jita = MarketLocation.objects.create(name="J", location_type="system", region_id=10000002)
    staging = MarketLocation.objects.create(name="S", location_type="system", region_id=10000002)
    sp = Stockpile.objects.create(name="hangar", location=staging)
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)

    generate_hauling_tasks(jita, staging)
    task = HaulingTask.objects.get(type_id=587)
    task.status = HaulingTask.Status.CLAIMED
    task.save()

    generate_hauling_tasks(jita, staging)  # shortfall persists, but task is claimed
    assert HaulingTask.objects.filter(type_id=587).count() == 1  # no duplicate


# --- market: empty order book writes no misleading row ---------------------
@pytest.mark.django_db
def test_empty_orders_writes_no_price():
    loc = MarketLocation.objects.create(name="J", location_type="system", region_id=10000002)
    assert update_price_from_orders(loc, 587, []) is None
    assert not MarketPrice.objects.filter(type_id=587).exists()


# --- discord webhook SSRF guard --------------------------------------------
@responses.activate
def test_discord_ssrf_guard_blocks_internal_url():
    # No responses registered: if it attempted the request it would error.
    notify._post_discord("http://169.254.169.254/latest/meta-data/", "secret")
    assert len(responses.calls) == 0


@responses.activate
def test_discord_allows_real_webhook():
    responses.add(responses.POST, "https://discord.com/api/webhooks/1/abc", status=204)
    notify._post_discord("https://discord.com/api/webhooks/1/abc", "hi")
    assert len(responses.calls) == 1


# --- erasure also removes PI plans + personal stockpiles -------------------
@pytest.mark.django_db
def test_delete_user_data_removes_personal_planning(sde, user):
    from apps.identity.services import delete_user_data
    from apps.skills.models import SkillPlan
    from apps.sso.models import EveCharacter

    character = EveCharacter.objects.create(character_id=1001, user=user, name="P", is_main=True)
    SkillPlan.objects.create(character=character, name="plan")
    Stockpile.objects.create(name="Personal", kind=Stockpile.Kind.PERSONAL, owner_character_id=1001)
    Recommendation.objects.create(
        type=Recommendation.Type.SKILL_TRAINING,
        subject_type="character",
        subject_id="1001",
        message="train",
        required_permission="member",
    )

    delete_user_data(user)
    assert SkillPlan.objects.filter(character=character).count() == 0
    assert Stockpile.objects.filter(owner_character_id=1001).count() == 0
    assert (
        Recommendation.objects.get(subject_id="1001").state == Recommendation.State.DISMISSED
    )
