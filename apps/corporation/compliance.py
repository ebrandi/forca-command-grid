"""Member compliance & inactivity report (the Corptools/Member-Audit "compliance" view).

Assembles data we already hold — corp roster (registration + last login) and the
ESI tokens (granted scopes) — into one actionable director list: who hasn't linked
a character, who's registered but missing a baseline scope (needs to re-authorise),
and who has gone inactive. No new ESI calls; pure read over existing tables.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

# Friendly labels for the baseline scopes every member is expected to grant, so the
# board can say *what* is missing rather than dumping raw scope strings.
_SCOPE_LABELS = {
    "esi-skills.read_skills.v1": "Skills",
    "esi-skills.read_skillqueue.v1": "Skill queue",
    "esi-killmails.read_killmails.v1": "Killmails",
    "esi-clones.read_implants.v1": "Implants",
    "esi-killmails.read_corporation_killmails.v1": "Corp killmails",
    "esi-corporations.read_corporation_membership.v1": "Corp membership",
    "esi-characters.read_corporation_roles.v1": "Corp roles",
}

DEFAULT_INACTIVE_DAYS = 30


def _required_scopes() -> set[str]:
    """Baseline scopes a compliant member should carry (publicData is implicit)."""
    return {s for s in getattr(settings, "EVE_SSO_DEFAULT_SCOPES", []) if s != "publicData"}


def compliance_report(inactive_days: int = DEFAULT_INACTIVE_DAYS) -> dict:
    """Per-member compliance rows + summary counts, worst offenders first."""
    from apps.sso.models import AuthToken, EveCharacter

    from .models import CorpMember, EveName

    members = list(CorpMember.objects.all())
    cids = [m.character_id for m in members]

    chars = {c.character_id: c for c in EveCharacter.objects.filter(character_id__in=cids)}
    token_scopes: dict[int, set[str]] = {}
    for t in AuthToken.objects.filter(
        character__character_id__in=cids, revoked_at__isnull=True
    ).select_related("character"):
        cid = t.character.character_id
        token_scopes.setdefault(cid, set()).update(t.scopes or [])
    names = dict(EveName.objects.filter(entity_id__in=cids).values_list("entity_id", "name"))

    required = _required_scopes()
    now = timezone.now()
    rows = []
    n_unregistered = n_missing = n_inactive = 0
    for m in members:
        char = chars.get(m.character_id)
        is_linked = bool(char and char.user_id)
        scopes = token_scopes.get(m.character_id)
        is_registered = is_linked and scopes is not None
        missing = sorted(
            _SCOPE_LABELS.get(s, s) for s in (required - scopes)
        ) if is_registered else []
        days_inactive = (now - m.logon_date).days if m.logon_date else None
        is_inactive = days_inactive is not None and days_inactive >= inactive_days

        if not is_registered:
            n_unregistered += 1
        if missing:
            n_missing += 1
        if is_inactive:
            n_inactive += 1

        rows.append({
            "character_id": m.character_id,
            "name": m.name or names.get(m.character_id) or (char.name if char else str(m.character_id)),
            "registered": is_registered,
            "linked_no_token": is_linked and scopes is None,
            "missing_scopes": missing,
            "days_inactive": days_inactive,
            "is_inactive": is_inactive,
            "compliant": is_registered and not missing and not is_inactive,
        })

    # Worst first: unregistered, then missing-scope, then inactive, then by name.
    def _rank(r):
        return (
            r["compliant"],            # compliant last
            r["registered"],           # unregistered first
            not r["missing_scopes"],   # missing-scope next
            not r["is_inactive"],
            r["name"].lower(),
        )

    rows.sort(key=_rank)
    return {
        "rows": rows,
        "total": len(rows),
        "unregistered": n_unregistered,
        "missing_scopes": n_missing,
        "inactive": n_inactive,
        "inactive_days": inactive_days,
        "compliant": sum(1 for r in rows if r["compliant"]),
    }
