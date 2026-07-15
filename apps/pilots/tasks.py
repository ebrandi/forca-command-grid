"""Scheduled pilot/leadership briefing delivery (Discord + email)."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.briefing")


@shared_task(name="pilots.deliver_leadership_briefing")
def deliver_leadership_briefing() -> str:
    """Ship the daily leadership briefing to Discord + email. No-op until channels exist."""
    from .briefing_delivery import deliver_leadership_briefing as _deliver

    result = _deliver()
    log.info(
        "briefing delivery: %s discord, %s email", result["discord"], result["email"]
    )
    return f"discord={result['discord']} email={result['email']}"


@shared_task(name="pilots.warm_briefings")
def warm_briefings() -> int:
    """Pre-warm every active pilot's Command Center caches so no viewer pays the
    cold recompute.

    The merged /dashboard/ stacks three formerly separate computations (digest,
    quest-log skills scan, onboarding milestones) on one request; cold, that is a
    multi-second page for the post-login landing page. Runs under the shortest
    TTL (digest/onboarding 10 min) so a visit almost always hits warm caches.
    (Readiness facets are warmed separately by readiness.warm_pilots.)
    """
    from django.core.cache import cache

    from apps.command_intel import pilot as ci_pilot
    from apps.onboarding.services import next_actions
    from apps.sso.models import EveCharacter
    from core.features import feature_enabled

    from .briefing import pilot_briefing

    warmed = 0
    mains = EveCharacter.objects.filter(
        is_corp_member=True, is_main=True, user__isnull=False
    ).select_related("user")
    for character in mains:
        user = character.user
        try:
            if feature_enabled("briefing"):
                # The digest is keyed by PILOT and by language (LP-3). This sweep warms each
                # account's MAIN — which is what ``pilot_briefing`` resolves to outside a
                # request — so the key it busts must name that same pilot, not the account.
                from core.i18n import i18n_cache_key

                cache.delete(
                    i18n_cache_key(f"briefing:pilot:v3:{character.character_id}")
                )
                pilot_briefing(user)
            if feature_enabled("command_intel_pilot"):
                ci_pilot.compute_directives(user, character, persist=True)
            if feature_enabled("onboarding"):
                onboarding = [
                    {"character": c, "action": action}
                    for c in user.characters.all()
                    for action in next_actions(c, limit=2)
                ]
                # Language-scoped: next_actions() now returns translated milestone
                # title/description, so a bare per-user key would freeze one reader's
                # locale onto everyone (mirrors the digest key above, LP-3).
                cache.set(i18n_cache_key(f"briefing:onboarding:{user.pk}"), onboarding, 600)
            if feature_enabled("operations"):
                # The dashboard's pinned next-op row (readiness per doctrine
                # fit ≈1s cold) — same delete-then-recompute idiom as digest.
                from apps.identity.views import _next_op_payload

                cache.delete(f"dashboard:next_op:{character.character_id}")
                _next_op_payload(character)
            warmed += 1
        except Exception:  # noqa: BLE001 - one pilot must not break the batch
            log.exception("briefing warm failed for %s", character.name)
    return warmed


@shared_task(name="pilots.warm_hall_of_fame")
def warm_hall_of_fame() -> str:
    """Pre-compute + cache the current Hall of Fame so no visitor pays the cold cost.

    The scoreboard aggregates millions of killmail-participant rows; with only a
    short read-cache, every few minutes one viewer would otherwise recompute it.
    Warming the current month (the one almost everyone views) keeps it instant.

    The cached payload is language-scoped (it embeds the month label, the category
    titles and the ``category_how`` one-liners), so the warm runs once per enabled locale
    under ``translation.override``. Without that loop this task — which runs under the
    default locale — would only ever fill the ``:en`` key and every non-English reader would
    take the cold recompute on each request (mirrors ``killboard.analytics.warm_caches``).
    """
    from django.utils import timezone, translation

    from core.i18n import enabled_locales

    from .halloffame import scoreboard

    now = timezone.now()
    for code in enabled_locales():
        with translation.override(code):
            scoreboard(now.year, now.month)
    return f"{now.year}-{now.month:02d}"


@shared_task(name="pilots.freeze_hof_months")
def freeze_hof_months() -> int:
    """Daily safety net (4.15): freeze every completed Hall-of-Fame month that lacks a
    weight snapshot at the current weights, so a month that closed since the last run is
    captured promptly (the weights-save console hook is the primary freeze point; this
    catches month rollovers that happen with no weight edit)."""
    from .halloffame import freeze_completed_months

    return freeze_completed_months()
