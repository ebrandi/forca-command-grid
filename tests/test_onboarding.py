"""Onboarding milestone auto-detection tests."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.fitparser import parse_eft
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.services import derive_skill_requirements
from apps.onboarding.models import OnboardingMilestone, OnboardingProgress
from apps.onboarding.services import evaluate_milestones, next_actions

RIFTER_EFT = "[Rifter, Tackle]\n200mm AutoCannon I\nDamage Control I\n"


@pytest.mark.django_db
def test_linked_and_skills_milestones(character):
    OnboardingMilestone.objects.create(key="link", title="Link", criteria={"type": "linked"}, sort_order=1)
    OnboardingMilestone.objects.create(
        key="skills", title="Import skills", criteria={"type": "skills_imported"}, sort_order=2
    )

    results = evaluate_milestones(character)
    statuses = {r["key"]: r["status"] for r in results}
    assert statuses["link"] == "done"  # a linked character satisfies it
    assert statuses["skills"] == "todo"  # no snapshot yet

    CharacterSkillSnapshot.objects.create(character=character, skills={}, is_latest=True)
    evaluate_milestones(character)
    assert (
        OnboardingProgress.objects.get(character=character, milestone__key="skills").status == "done"
    )


@pytest.mark.django_db
def test_doctrine_ready_milestone(sde, character):
    cat = DoctrineCategory.objects.create(key="tk", label="Tackle")
    doctrine = Doctrine.objects.create(name="Tackle", category=cat)
    parsed = parse_eft(RIFTER_EFT)
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name="Tackle", ship_type_id=parsed["ship_type_id"], modules=parsed["modules"]
    )
    derive_skill_requirements(fit)
    OnboardingMilestone.objects.create(
        key="fly",
        title="Fly tackle",
        criteria={"type": "doctrine_ready", "doctrine_id": doctrine.id},
        sort_order=1,
    )

    # Not ready without skills.
    evaluate_milestones(character)
    assert OnboardingProgress.objects.get(character=character, milestone__key="fly").status == "todo"

    # Train the required skills -> milestone auto-completes.
    CharacterSkillSnapshot.objects.create(
        character=character,
        skills={"3331": {"trained_level": 5}, "3301": {"trained_level": 5}, "3300": {"trained_level": 5}},
        is_latest=True,
    )
    evaluate_milestones(character)
    assert OnboardingProgress.objects.get(character=character, milestone__key="fly").status == "done"


@pytest.mark.django_db
def test_next_actions_lists_incomplete(character):
    OnboardingMilestone.objects.create(
        key="skills", title="Import skills", criteria={"type": "skills_imported"}, sort_order=1
    )
    actions = next_actions(character)
    assert any(a["key"] == "skills" for a in actions)


# --- the leader-configurable rework ------------------------------------------
@pytest.mark.django_db
def test_default_milestones_and_glossary_seeded(db):
    """The data migration ships a usable default journey + glossary — create-only."""
    from apps.onboarding.models import GlossaryTerm

    assert OnboardingMilestone.objects.filter(key="join-comms", active=True).exists()
    assert OnboardingMilestone.objects.filter(key="move-to-staging").exists()
    for term in ("Doctrine", "ISK", "Cyno", "Highsec / Lowsec / Nullsec"):
        assert GlossaryTerm.objects.filter(term=term).exists(), term


@pytest.mark.django_db
def test_scopes_and_doctrine_any_criteria(character):
    from apps.onboarding.services import _criterion_met, is_manual
    from apps.sso.models import AuthToken

    crit = {"type": "scopes", "scopes": ["esi-assets.read_assets.v1"]}
    assert not is_manual(crit)
    assert not _criterion_met(character, crit)
    AuthToken.objects.create(character=character, scopes=["esi-assets.read_assets.v1", "publicData"])
    assert _criterion_met(character, crit)
    # doctrine_any without a snapshot is not met; manual detection for empty criteria.
    assert not _criterion_met(character, {"type": "doctrine_any"})
    assert is_manual({}) and is_manual(None) and not is_manual({"type": "linked"})


@pytest.mark.django_db
def test_manual_milestone_mark_done_and_undo(client, character):
    m = OnboardingMilestone.objects.create(key="comms-t", title="Join comms", criteria={}, sort_order=1)
    client.force_login(character.user)
    client.post(f"/onboarding/milestone/{m.pk}/")
    p = OnboardingProgress.objects.get(character=character, milestone=m)
    assert p.status == "done" and not p.auto_detected
    client.post(f"/onboarding/milestone/{m.pk}/", {"action": "undo"})
    p.refresh_from_db()
    assert p.status == "todo"


@pytest.mark.django_db
def test_auto_milestone_cannot_be_hand_ticked(client, character):
    m = OnboardingMilestone.objects.create(
        key="auto-t", title="Import skills", criteria={"type": "skills_imported"}, sort_order=1
    )
    client.force_login(character.user)
    client.post(f"/onboarding/milestone/{m.pk}/")
    p = OnboardingProgress.objects.filter(character=character, milestone=m).first()
    assert p is None or p.status != "done"


@pytest.mark.django_db
def test_member_page_renders_journey_guide_and_glossary(client, character):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    RoleAssignment.objects.create(user=character.user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(character.user)
    html = client.get("/onboarding/").content.decode()
    assert "Welcome to nullsec" in html
    assert "The path" in html and "Mark done" in html      # journey with manual check-off
    assert "survival guide" in html and "Ships are ammo" in html
    assert "Glossary" in html and "Nullsec" in html
    assert "Your journey" in html                          # progress bar block
    # Nav: New Player now lives in the Pilot group, above the Community group.
    assert html.index("New Player</a>") < html.index('data-acc-key="community"')
