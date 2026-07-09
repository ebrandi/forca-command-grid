"""Admin & audit services: data-retention enforcement."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot, SkillQueueSnapshot
from apps.market.models import MarketOrderSnapshot

from .models import AppSetting, AuditLog, DataRetentionPolicy

# The audit trail (officer access to member data, role changes, character detaches)
# must stay investigable, so it is never pruned below this floor even if a retention
# policy is configured shorter — "a protected retention floor".
AUDIT_RETENTION_FLOOR_DAYS = 730


def enforce_retention() -> dict:
    """Delete data older than each active retention policy's window.

    Killmails are never deleted (public EVE facts). The latest skill snapshot
    per character is always kept; only superseded history is pruned.
    """
    removed: dict[str, int] = {}
    now = timezone.now()
    for policy in DataRetentionPolicy.objects.filter(active=True):
        cutoff = now - timedelta(days=policy.retention_days)
        if policy.data_class == DataRetentionPolicy.DataClass.SKILL_SNAPSHOT:
            removed["skill_snapshot"] = CharacterSkillSnapshot.objects.filter(
                as_of__lt=cutoff, is_latest=False
            ).delete()[0]
            SkillQueueSnapshot.objects.filter(as_of__lt=cutoff, is_latest=False).delete()
        elif policy.data_class == DataRetentionPolicy.DataClass.MARKET_SNAPSHOT:
            removed["market_snapshot"] = MarketOrderSnapshot.objects.filter(
                as_of__lt=cutoff
            ).delete()[0]
        elif policy.data_class == DataRetentionPolicy.DataClass.AUDIT:
            # Honour the protected floor: never prune audit rows younger than it, even
            # if the policy sets a shorter window.
            audit_cutoff = now - timedelta(
                days=max(policy.retention_days, AUDIT_RETENTION_FLOOR_DAYS)
            )
            removed["audit"] = AuditLog.objects.filter(created_at__lt=audit_cutoff).delete()[0]
    return removed


# The on-member-leave sweep is destructive, so it ships DISARMED: until a Director arms
# it on the retention page it runs in report-only mode (counts what it *would* delete,
# deletes nothing). This AppSetting is the arm switch + the last report.
_ON_LEAVE_ARMED_KEY = "retention:on_leave_armed"
_ON_LEAVE_REPORT_KEY = "retention:last_leave_report"

# Data classes the leave sweep can act on, mapped to a delete callable that removes the
# given departed characters' rows and returns the count. AUDIT/MARKET are corp-level
# (not member data) and are deliberately excluded from the leave sweep.
_LEAVE_CLASSES = ("token", "skill_snapshot", "asset_snapshot")


def on_leave_armed() -> bool:
    row = AppSetting.objects.filter(key=_ON_LEAVE_ARMED_KEY).first()
    val = row.value if row and isinstance(row.value, dict) else {}
    return bool(val.get("armed"))


def _departed_character_ids() -> list[int]:
    """Characters that no longer belong to an *active* corp-member account.

    A member who leaves keeps their linked account but loses ``is_corp_member``; we only
    act once the whole account has left (no remaining corp-member character), so an active
    member's departed alt is never swept. Scoped to linked characters, so killboard enemy
    rows (``is_corp_member=False`` but never synced) are not scanned.
    """
    from apps.sso.models import EveCharacter

    active_user_ids = set(
        EveCharacter.objects.filter(is_corp_member=True, user__isnull=False)
        .values_list("user_id", flat=True)
    )
    return list(
        EveCharacter.objects.filter(is_corp_member=False, user__isnull=False)
        .exclude(user_id__in=active_user_ids)
        .values_list("character_id", flat=True)
    )


def enforce_member_leave(dry_run: bool | None = None) -> dict:
    """Apply each data class's ``on_member_leave`` policy to departed members' data.

    Honours the configured mode exactly: ``DELETE`` removes the departed characters'
    rows of that class; ``RETAIN`` (and ``ANONYMISE``, which has no faithful in-place
    form for these intrinsically per-character classes) keeps them. Runs report-only
    unless armed — in report mode it counts what *would* be deleted and writes nothing.
    Every run is audited and the report is stored for the retention page. Idempotent.
    """
    from apps.sso.models import AuthToken, EveScopeGrant
    from apps.stockpile.models import Asset

    armed = on_leave_armed()
    dry = (not armed) if dry_run is None else dry_run
    departed_ids = _departed_character_ids()
    policies = {
        p.data_class: p
        for p in DataRetentionPolicy.objects.filter(data_class__in=_LEAVE_CLASSES)
    }

    def _wants_delete(data_class: str) -> bool:
        p = policies.get(data_class)
        return bool(p and p.active and p.on_member_leave == DataRetentionPolicy.OnLeave.DELETE)

    from apps.characters.models import CharacterSkillSnapshot, SkillQueueSnapshot

    # Count first (what would be / is deleted), so the report is computed the same way in
    # both modes; then, only when armed, run every delete inside one transaction so a
    # crash can't leave a class half-pruned (all-or-nothing per sweep).
    counts: dict[str, int] = {}
    if departed_ids:
        if _wants_delete("token"):
            counts["token"] = AuthToken.objects.filter(character_id__in=departed_ids).count()
        if _wants_delete("skill_snapshot"):
            counts["skill_snapshot"] = CharacterSkillSnapshot.objects.filter(
                character_id__in=departed_ids
            ).count()
        if _wants_delete("asset_snapshot"):
            counts["asset_snapshot"] = Asset.objects.filter(
                owner_type=Asset.Owner.CHARACTER, owner_id__in=departed_ids
            ).count()

    if not dry and departed_ids and any(counts.values()):
        with transaction.atomic():
            if counts.get("token"):
                EveScopeGrant.objects.filter(character_id__in=departed_ids).delete()
                AuthToken.objects.filter(character_id__in=departed_ids).delete()
            if counts.get("skill_snapshot"):
                SkillQueueSnapshot.objects.filter(character_id__in=departed_ids).delete()
                CharacterSkillSnapshot.objects.filter(character_id__in=departed_ids).delete()
            if counts.get("asset_snapshot"):
                Asset.objects.filter(
                    owner_type=Asset.Owner.CHARACTER, owner_id__in=departed_ids
                ).delete()

    report = {
        "at": timezone.now().isoformat(),
        "dry_run": dry,
        "armed": armed,
        "departed_accounts": len(departed_ids),
        "counts": counts,
    }
    AppSetting.objects.update_or_create(key=_ON_LEAVE_REPORT_KEY, defaults={"value": report})
    from core.audit import audit_log

    audit_log(
        None,
        "retention.member_leave.report" if dry else "retention.member_leave.enforced",
        target_type="data_retention_policy",
        metadata={"departed_accounts": len(departed_ids), "counts": counts, "armed": armed},
    )
    return report
