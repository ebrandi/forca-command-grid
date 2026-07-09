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
