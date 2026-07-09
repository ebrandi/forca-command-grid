"""Smoke test: every authenticated page renders (catches template errors)."""
from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_all_pages_render(client, django_user_model, sde):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
    from apps.identity.models import RoleAssignment
    from apps.industry.models import IndustryProject, IndustryProjectItem
    from apps.recommendations.models import Recommendation
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role
    from apps.stockpile.models import Stockpile, StockpileItem
    from core import rbac

    user = django_user_model.objects.create(username="director")
    for role in (rbac.ROLE_MEMBER, rbac.ROLE_OFFICER, rbac.ROLE_DIRECTOR):
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    character = EveCharacter.objects.create(character_id=42, user=user, name="Pilot", is_main=True)
    client.force_login(user)

    project = IndustryProject.objects.create(name="Build Rifters")
    IndustryProjectItem.objects.create(project=project, type_id=587, quantity=2)
    sp = Stockpile.objects.create(name="Staging")
    StockpileItem.objects.create(stockpile=sp, type_id=587, quantity_current=4, quantity_target=40)
    cat = DoctrineCategory.objects.create(key="tk", label="Tackle")
    doctrine = Doctrine.objects.create(name="Tackle", category=cat)
    DoctrineFit.objects.create(doctrine=doctrine, name="Tackle", ship_type_id=587)

    from apps.skills.models import SkillPlan, SkillPlanStep
    plan = SkillPlan.objects.create(character=character, name="Fly Tackle", target_doctrine=doctrine)
    SkillPlanStep.objects.create(plan=plan, order=0, skill_type_id=3300, target_level=3, estimated_seconds=3600)

    from apps.killboard.models import Watchlist, WatchlistEntry
    wl = Watchlist.objects.create(name="Hostiles", purpose="watch")
    WatchlistEntry.objects.create(watchlist=wl, entity_type="corporation", entity_id=500)
    Recommendation.objects.create(
        type=Recommendation.Type.STOCK_SHORTAGE, subject_type="type", subject_id="587",
        message="Build or buy 36", required_permission="officer",
    )

    pages = [
        "/", "/onboarding/", "/killboard/", "/doctrines/", "/doctrines/my-readiness/",
        f"/doctrines/{doctrine.pk}/", f"/doctrines/{doctrine.pk}/readiness/",
        f"/doctrines/{doctrine.pk}/prep/", f"/doctrines/{doctrine.pk}/supply/",
        "/dashboard/", f"/characters/{character.character_id}/", "/privacy/", "/auth/eve/scopes/",
        "/industry/", "/industry/guide/", "/industry/calculator/", "/industry/invention/",
        "/industry/chain/", "/industry/blueprints/", "/industry/jobs/", "/industry/demand/",
        "/industry/plans/", "/industry/plans/new/", f"/industry/plans/{project.pk}/",
        "/skills/gap/", f"/skills/{plan.pk}/",  # /skills/ itself now redirects to the character page
        "/killboard/intel/", f"/killboard/intel/{wl.pk}/",
        "/stockpile/", "/stockpile/assets/", "/stockpile/assets/?owner=corp", "/stockpile/logistics/",
        "/market/", "/recommendations/officer/",
        "/pilots/contributions/", "/tasks/", "/srp/", "/srp/queue/",
        "/ops/audit/", "/ops/health/", "/readiness/", "/operations/", "/kb/", "/kb/new/",
        "/recruitment/", "/roster/",
        "/ops/admin/", "/ops/admin/members/", "/ops/admin/doctrines/",
        f"/ops/admin/doctrines/{doctrine.pk}/", "/ops/admin/content/", "/ops/admin/settings/",
    ]
    for url in pages:
        resp = client.get(url)
        assert resp.status_code == 200, f"{url} returned {resp.status_code}"

    # Absorbed into the Command Center — old bookmarks redirect there.
    for url in ("/recommendations/mine/", "/command/me/", "/pilots/briefing/", "/readiness/me/"):
        resp = client.get(url)
        assert resp.status_code == 302, f"{url} returned {resp.status_code}"
        assert resp["Location"] == "/dashboard/"

    # /erp/ (Production) now folds into the Industry Center Job Tracker.
    erp = client.get("/erp/")
    assert erp.status_code == 302 and erp["Location"] == "/industry/jobs/"
