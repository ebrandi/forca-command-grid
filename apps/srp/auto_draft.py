"""SRP auto-draft from attendance + losses (4.6).

When leadership arms it, auto-draft a SUBMITTED SRP claim for each eligible loss (a loss on
a sanctioned op the pilot attended, when the programme requires it) — so the pilot doesn't
have to file it and no eligible loss is forgotten. It **never auto-pays**: a draft is still
a SUBMITTED claim needing officer approval, so separation of duties is preserved. Future-
only (only losses after ``auto_draft_since``) and only for **enrolled** pilots holding a
**valid ESI token**, mirroring the platform's reward-integrity posture.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

log = logging.getLogger("forca.srp")
_MAX_SCAN_DAYS = 21  # never scan further back than this, even if the baseline is older


def auto_draft_claims(*, limit: int = 200) -> dict:
    """One sweep: draft claims for eligible, enrolled+valid-ESI, unclaimed losses since the
    baseline. Inert unless the programme is enabled AND auto-draft is armed."""
    from apps.killboard.models import Killmail
    from apps.sso.models import AuthToken, EveCharacter

    from .services import active_program, eligibility

    program = active_program()
    if not (program.enabled and program.auto_draft_enabled):
        return {"status": "disabled"}
    now = timezone.now()
    baseline = program.auto_draft_since or now
    since = max(baseline, now - dt.timedelta(days=_MAX_SCAN_DAYS))  # future-only + bounded scan

    # Select the oldest UNCLAIMED losses (the srp_claims__isnull filter runs in SQL, BEFORE
    # the limit) so the sweep always makes progress — otherwise once >=limit claimed losses
    # accrue in the window it would fetch those, draft 0, and starve newer losses (review MED).
    losses = list(
        Killmail.objects.filter(
            involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
            killmail_time__gte=since, srp_claims__isnull=True,
        ).prefetch_related("items").order_by("killmail_time")[:limit]
    )
    victim_ids = {km.victim_character_id for km in losses if km.victim_character_id}
    if not victim_ids:
        return {"drafted": 0}

    # victim character → enrolled user, and which of those users hold a valid ESI token
    # (non-revoked AND still carrying a refresh token — exact parity with AuthToken.is_valid).
    char_user = dict(
        EveCharacter.objects.filter(character_id__in=victim_ids, user__isnull=False)
        .values_list("character_id", "user_id")
    )
    users_with_token = set(
        AuthToken.objects.filter(
            character__user_id__in=set(char_user.values()), revoked_at__isnull=True
        ).exclude(_refresh_token="").values_list("character__user_id", flat=True)
    )

    drafted = 0
    for km in losses:
        user_id = char_user.get(km.victim_character_id)
        if user_id is None or user_id not in users_with_token:
            continue  # not enrolled, or no valid ESI token → pilot can still submit manually
        info = eligibility(km, program)
        if not info.get("eligible"):
            continue
        if _draft_one(km, user_id, info):
            drafted += 1
    return {"drafted": drafted}


def _draft_one(km, user_id: int, info: dict) -> bool:
    from django.contrib.auth import get_user_model
    from django.db import IntegrityError, transaction

    from .services import _persist_claim

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return False
    try:
        with transaction.atomic():
            _persist_claim(km, user, info, auto_drafted=True, notify=False)
    except IntegrityError:
        return False  # a concurrent manual submit won the per-killmail unique constraint
    except Exception:  # noqa: BLE001 - one bad loss must not abort the sweep
        log.exception("SRP auto-draft failed for killmail %s", km.killmail_id)
        return False
    return True
