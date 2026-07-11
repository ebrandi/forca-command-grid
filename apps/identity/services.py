"""Identity services: member self-service data deletion (GDPR erasure)."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.characters.models import (
    CharacterAttributes,
    CharacterSkillSnapshot,
    SkillQueueSnapshot,
)
from apps.onboarding.models import OnboardingProgress
from apps.recommendations.models import Recommendation
from apps.skills.models import SkillPlan
from apps.sso.models import AuthToken, EveScopeGrant
from apps.stockpile.models import Stockpile
from core.audit import audit_log


@transaction.atomic
def delete_user_data(user, actor=None, ip: str = "") -> dict:
    """Erase a user's private EVE data and detach their characters.

    Killmails are public EVE facts and are retained (they reference character
    ids, not our account). Private snapshots, tokens, plans and progress are
    deleted; characters are detached from the account.
    """
    characters = list(user.characters.all())
    char_ids = [c.character_id for c in characters]
    summary = {"characters": len(characters), "deleted": {}}

    for character in characters:
        summary["deleted"]["skills"] = CharacterSkillSnapshot.objects.filter(
            character=character
        ).delete()[0]
        SkillQueueSnapshot.objects.filter(character=character).delete()
        CharacterAttributes.objects.filter(character=character).delete()
        SkillPlan.objects.filter(character=character).delete()
        OnboardingProgress.objects.filter(character=character).delete()
        AuthToken.objects.filter(character=character).delete()
        EveScopeGrant.objects.filter(character=character).delete()
        # Personal planning data keyed by character id.
        Stockpile.objects.filter(owner_character_id=character.character_id).delete()
        # Close any open recommendations that single out this person.
        Recommendation.objects.filter(
            subject_type="character",
            subject_id=str(character.character_id),
            state__in=[Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED],
        ).update(state=Recommendation.State.DISMISSED, closed_at=timezone.now())
        character.user = None
        character.is_main = False
        character.is_corp_member = False
        character.save(update_fields=["user", "is_main", "is_corp_member"])

    # Personal engagement data keyed by the account (not by character).
    from apps.pilots.models import ContributionEvent, PilotPreference

    summary["deleted"]["contributions"] = ContributionEvent.objects.filter(user=user).delete()[0]
    PilotPreference.objects.filter(user=user).delete()

    # Capsuleer Path career-planning data (account-scoped personal data — brief §4, doc 09 §7.3).
    # No auto-discovery exists, so every model is deleted explicitly, children first for accurate
    # counts (CASCADE would cover them). Corp CareerTemplate rows survive (created_by is SET_NULL);
    # a mentor note this user left on another pilot's goal is SET_NULL, not deleted — the note was
    # authored for that goal's owner and is theirs to keep.
    from apps.capsuleer.models import (
        CareerActionStep,
        CareerGoal,
        CareerMilestone,
        CareerProfile,
        GoalActivity,
        PathSuggestion,
        ProgressSnapshot,
    )

    capsuleer_counts = {
        "path_suggestions": PathSuggestion.objects.filter(user=user).delete()[0],
        "goal_activity": GoalActivity.objects.filter(goal__user=user).delete()[0],
        "progress_snapshots": ProgressSnapshot.objects.filter(goal__user=user).delete()[0],
        "action_steps": CareerActionStep.objects.filter(goal__user=user).delete()[0],
        "milestones": CareerMilestone.objects.filter(goal__user=user).delete()[0],
        "goals": CareerGoal.objects.filter(user=user).delete()[0],
        "profile": CareerProfile.objects.filter(user=user).delete()[0],
        # A note/endorsement this user left on ANOTHER pilot's goal survives for that goal's owner,
        # but the authorship link is severed (SET_NULL never fires because the User row is not
        # deleted — the account is only logged out; doc 09 §7.3).
        "authored_activity_anonymised": GoalActivity.objects.filter(actor=user)
        .exclude(goal__user=user).update(actor=None),
    }
    summary["capsuleer"] = capsuleer_counts

    user.main_character_id = None
    user.save(update_fields=["main_character_id"])

    # Characters are now detached, so auto-managed roles (member/director) no
    # longer have any backing — reconcile them away rather than leaving a
    # role-bearing but character-less account.
    from apps.sso.services import sync_roles_for_user

    sync_roles_for_user(user)

    audit_log(
        actor or user,
        "user.data_deleted",
        target_type="user",
        target_id=str(user.id),
        metadata={"characters": char_ids},
        ip=ip,
    )
    return summary
