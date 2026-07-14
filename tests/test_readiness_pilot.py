"""Phase 4 — per-pilot readiness, quest log, and the /readiness/me/ dashboard."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.readiness.models import PilotReadinessSnapshot, PilotRecommendation
from apps.readiness.pilot import compute_pilot
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _doctrine(name="Core", priority=100, req_level=3):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


def _pilot(django_user_model, cid=9001, gunnery_level=5, role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    ch = EveCharacter.objects.create(
        character_id=cid, user=user, name=f"P{cid}", is_main=True, is_corp_member=True
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}}
    )
    return user, ch


# --- facets ------------------------------------------------------------------
@pytest.mark.django_db
def test_compute_pilot_doctrine_facet_and_snapshot(django_user_model, sde):
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=5)  # can fly
    result = compute_pilot(ch)
    assert result["facets"]["doctrine"] == 100  # flies the only known doctrine
    assert 0 <= result["overall"] <= 100
    # A snapshot is persisted with the facet payload.
    snap = PilotReadinessSnapshot.objects.get(character_id=ch.character_id)
    assert snap.overall == result["overall"]
    assert snap.facets["doctrine"] == 100


@pytest.mark.django_db
def test_doctrine_facet_zero_when_cannot_fly(django_user_model, sde):
    _doctrine("Core", 100, 3)
    _, ch = _pilot(django_user_model, gunnery_level=1)  # known, can't fly
    assert compute_pilot(ch)["facets"]["doctrine"] == 0


@pytest.mark.django_db
def test_compute_pilot_always_produces_a_quest(django_user_model, sde):
    # Flies everything → the log still seeds a positive "stay current" quest.
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=5)
    compute_pilot(ch)
    assert PilotRecommendation.objects.filter(user=user).exists()


# --- recommendation state preservation ---------------------------------------
@pytest.mark.django_db
def test_recommendation_state_preserved_across_regeneration(django_user_model, sde):
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=5)
    compute_pilot(ch)
    reco = PilotRecommendation.objects.filter(user=user).first()
    reco.state = PilotRecommendation.State.DONE
    reco.save()

    compute_pilot(ch)  # regenerate — display refreshes, state must persist
    reco.refresh_from_db()
    assert reco.state == PilotRecommendation.State.DONE
    # And no duplicate row for the same (category, ref_type, ref_id).
    assert PilotRecommendation.objects.filter(
        user=user, category=reco.category, ref_type=reco.ref_type, ref_id=reco.ref_id
    ).count() == 1


@pytest.mark.django_db
def test_stale_open_recommendation_is_dropped(django_user_model, sde):
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=5)
    compute_pilot(ch)
    # An OPEN reco the engine no longer generates (gap closed) is removed…
    stale = PilotRecommendation.objects.create(
        user=user, character_id=ch.character_id,
        category=PilotRecommendation.Category.SHIP, ref_type="doctrine",
        ref_id="99999", title="Old", detail="", priority=1, points=1,
    )
    # …but a DONE one is preserved as history.
    done = PilotRecommendation.objects.create(
        user=user, character_id=ch.character_id,
        category=PilotRecommendation.Category.SKILL, ref_type="doctrine",
        ref_id="88888", title="Done", detail="", priority=1, points=1,
        state=PilotRecommendation.State.DONE,
    )
    compute_pilot(ch)
    assert not PilotRecommendation.objects.filter(pk=stale.pk).exists()
    assert PilotRecommendation.objects.filter(pk=done.pk).exists()


# --- dashboard + actions -----------------------------------------------------
@pytest.mark.django_db
def test_pilot_dashboard_redirects_to_the_command_center(client, django_user_model, sde):
    # /readiness/me/ was absorbed into /dashboard/ — following the redirect must
    # land on the merged page with the readiness panel and quest queue rendered.
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=5)
    client.force_login(user)
    resp = client.get("/readiness/me/", follow=True)
    assert resp.redirect_chain[-1][0] == "/dashboard/"
    html = resp.content.decode()
    assert "Pilot stats" in html
    assert "Quest log" in html


@pytest.mark.django_db
def test_reco_action_snooze_and_dismiss(client, django_user_model, sde):
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=5)
    compute_pilot(ch)
    reco = PilotRecommendation.objects.filter(user=user).first()
    client.force_login(user)

    client.post(f"/readiness/me/reco/{reco.id}/", {"action": "snooze"})
    reco.refresh_from_db()
    assert reco.snoozed_until is not None

    client.post(f"/readiness/me/reco/{reco.id}/", {"action": "dismiss"})
    reco.refresh_from_db()
    assert reco.state == PilotRecommendation.State.DISMISSED


@pytest.mark.django_db
def test_reco_action_cannot_touch_another_users_reco(client, django_user_model, sde):
    _doctrine("Core", 100, 3)
    owner, ch = _pilot(django_user_model, cid=9001, gunnery_level=5)
    compute_pilot(ch)
    reco = PilotRecommendation.objects.filter(user=owner).first()

    attacker, _ = _pilot(django_user_model, cid=9002, gunnery_level=5)
    client.force_login(attacker)
    resp = client.post(f"/readiness/me/reco/{reco.id}/", {"action": "dismiss"})
    assert resp.status_code == 404  # not your recommendation
    reco.refresh_from_db()
    assert reco.state == PilotRecommendation.State.OPEN


@pytest.mark.django_db
def test_done_reco_clears_from_active_quest_log(client, django_user_model, sde):
    from core.features import set_disabled

    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=1)  # has trainable doctrines
    compute_pilot(ch, persist=True)
    reco = PilotRecommendation.objects.filter(user=user).first()
    client.force_login(user)
    # CI off so the queue shows readiness quests only (its train-into card would
    # otherwise legitimately carry the same title as the readiness one).
    set_disabled(["command_intel_pilot"])
    html = client.get("/dashboard/").content.decode()
    assert reco.title in html
    client.post(f"/readiness/me/reco/{reco.id}/", {"action": "done"})
    # A done item no longer appears in the active quest queue.
    html = client.get("/dashboard/").content.decode()
    assert reco.title not in html
    set_disabled([])


@pytest.mark.django_db
def test_get_is_idempotent_once_warmed(client, django_user_model, sde):
    # Once the pilot has been warmed (recos exist), a plain GET of the merged
    # Command Center writes no new snapshot (the beat owns persistence).
    _doctrine("Core", 100, 3)
    user, ch = _pilot(django_user_model, gunnery_level=1)
    compute_pilot(ch, persist=True)  # warm: one snapshot + seed recos
    before = PilotReadinessSnapshot.objects.count()
    client.force_login(user)
    from django.core.cache import cache
    cache.clear()  # force the view's cache-miss path
    client.get("/dashboard/")
    client.get("/dashboard/")
    assert PilotReadinessSnapshot.objects.count() == before  # no write-on-GET


@pytest.mark.django_db
def test_warm_pilots_scores_active_mains(django_user_model, sde):
    from apps.readiness.tasks import warm_pilots

    _doctrine("Core", 100, 3)
    _pilot(django_user_model, cid=9001, gunnery_level=5)
    _pilot(django_user_model, cid=9002, gunnery_level=1)
    assert warm_pilots() == 2
    assert PilotReadinessSnapshot.objects.count() == 2
