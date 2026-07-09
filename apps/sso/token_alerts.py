"""SSO-2 (roadmap 2.1) — ingestion-token liveness alerts.

A corp-ingestion token — one carrying a **director** corp scope that powers a corp
data feed (assets, wallet, roster, structures, industry, contracts, …) — that dies
silently stalls that feed, and today only shows up on the ``/ops`` health page. This
fires **one** nudge to the token's owner the moment it genuinely dies, naming the
character and the corp data at risk, and re-arms when the token is healthy again.

Two false-positive traps this deliberately avoids:

* **Default login tokens.** Every member's baseline login also grants a few
  ``…corporation…`` scopes (corp killmails/membership/roles), so a substring match
  would misclassify the whole membership. Classification is against a curated set of
  *director* ingestion scopes, minus the default login set.
* **Superseded tokens.** ``revoked_at`` is set on perfectly healthy tokens that were
  merely superseded by a newer/wider grant (at login and by the prune beat). A token
  is only "dead" here if **no other live token for the same character still covers**
  its ingestion scopes — i.e. ingestion is actually stalled, not just re-issued.

Keyed on the permanent death state (revoked, or ``refresh_fail_count`` at/above the
revoke threshold) — never a single transient CCP 400. Scoped to token liveness so it
never double-alerts with 2.2's stale-feed / CVE infra digest.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.admin_audit.models import AppSetting

log = logging.getLogger("forca.sso")

_EVENT_KEY = "sso.ingestion_token_dead"
# A refresh_fail_count at/above this is a permanent death (matches the revoke policy).
_REVOKE_THRESHOLD = 3

# Director-only corp scopes whose loss actually stalls a corp ingestion feed. NOT the
# baseline login scopes every member grants (those carry `corporation` too, but the
# corp feeds run off the director token, so a member losing them stalls nothing).
_CORP_INGESTION_SCOPES = frozenset({
    "esi-assets.read_corporation_assets.v1",
    "esi-wallet.read_corporation_wallets.v1",
    "esi-corporations.track_members.v1",
    "esi-corporations.read_structures.v1",
    "esi-corporations.read_blueprints.v1",
    "esi-corporations.read_contacts.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-industry.read_corporation_mining.v1",
    "esi-contracts.read_corporation_contracts.v1",
})

# Map an ingestion scope to a human "what's at risk" area.
_SCOPE_AREAS = [
    ("assets", "assets"),
    ("wallet", "wallet"),
    ("track_members", "roster"),
    ("structures", "structures"),
    ("blueprints", "blueprints"),
    ("contacts", "contacts"),
    ("industry", "industry jobs"),
    ("mining", "mining ledger"),
    ("contracts", "contracts"),
]


def _ingestion_scope_set() -> frozenset[str]:
    """The curated ingestion scopes minus the default login set, so a member's baseline
    token (which also carries corporation-scoped defaults) is never misclassified."""
    default = set(getattr(settings, "EVE_SSO_DEFAULT_SCOPES", ()) or ())
    return frozenset(_CORP_INGESTION_SCOPES - default)


def _corp_ingestion_scopes(scopes) -> set[str]:
    return set(scopes or []) & _ingestion_scope_set()


def _is_dead(token) -> bool:
    return token.revoked_at is not None or token.refresh_fail_count >= _REVOKE_THRESHOLD


def _still_covered(token, ingestion_scopes: set[str]) -> bool:
    """True if another *live* token for this character still covers these ingestion
    scopes — so the revoke/failure is a supersede/duplicate, not a real death."""
    from apps.sso.models import AuthToken

    live_union: set[str] = set()
    for other in AuthToken.objects.filter(
        character_id=token.character_id, revoked_at__isnull=True,
        refresh_fail_count__lt=_REVOKE_THRESHOLD,
    ).exclude(id=token.id):
        live_union |= set(other.scopes or [])
    return ingestion_scopes.issubset(live_union)


def _areas_at_risk(ingestion_scopes: set[str]) -> str:
    out: list[str] = []
    for token_word, label in _SCOPE_AREAS:
        if any(token_word in s for s in ingestion_scopes) and label not in out:
            out.append(label)
    return ", ".join(out) or "corp data"


def _emit_death(token, ingestion_scopes: set[str]) -> bool:
    char = token.character
    char_name = char.name if char else str(token.character_id)
    user_id = char.user_id if char else None
    # Deliver to the owner who can re-grant it; fall back to the director role only if
    # the token isn't linked to a user (so the downtime is still seen by someone).
    audience = {"kind": "user", "id": user_id} if user_id else {"kind": "director"}
    areas = _areas_at_risk(ingestion_scopes)
    # Per-firing microsecond stamp so a genuine re-death after a recovery isn't swallowed
    # by pingboard's idempotency/duplicate guard.
    stamp = int(timezone.now().timestamp() * 1_000_000)
    body = (
        f"The ESI token for {char_name} that authorises corp data ingestion ({areas}) "
        "has stopped working — it was revoked or failed to refresh past the retry limit. "
        "Re-authorise it on the ESI scopes page (/auth/scopes/) to resume ingestion; "
        f"corp {areas} pages will show stale data until then."
    )
    try:
        from apps.pingboard import services as pingboard

        alert = pingboard.emit_broadcast(
            category="custom",
            title=f"Re-authorise {char_name}: corp data ingestion stopped",
            body=body, audience=audience, source_service="sso",
            source_object_id=f"tokdeath:{token.id}:{stamp}",
            idempotency_key=f"sso:tokdeath:{token.id}:{stamp}",
        )
        return alert is not None
    except Exception:  # noqa: BLE001 — a notification fault must never break the scan
        log.exception("ingestion-token death alert failed for token %s", token.id)
        return False


def _reconcile_markers() -> int:
    """Delete dedup markers whose token has recovered, become covered by a live token, or
    no longer exists — re-arming a future death and preventing orphan accumulation."""
    from apps.sso.models import AuthToken

    cleared = 0
    for setting in AppSetting.objects.filter(key__startswith="sso:tokdeath:"):
        try:
            tid = int(setting.key.rsplit(":", 1)[-1])
        except (ValueError, TypeError):
            setting.delete()
            cleared += 1
            continue
        token = AuthToken.objects.select_related("character").filter(id=tid).first()
        if token is None:
            setting.delete()
            cleared += 1
            continue
        ing = _corp_ingestion_scopes(token.scopes)
        if not ing or not _is_dead(token) or _still_covered(token, ing):
            setting.delete()
            cleared += 1
    return cleared


def scan_ingestion_tokens() -> dict:
    """Nudge the owner of each corp-ingestion token that has just genuinely died; re-arm
    on recovery. One alert per death (an ``AppSetting`` marker per token is the dedup
    state). No-op when leadership disables the event.
    """
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_EVENT_KEY):
        return {"status": "disabled"}

    from django.db.models import Q

    from apps.sso.models import AuthToken

    alerted = 0
    # Only consider genuinely-dead rows (revoked or past the refresh limit) — a cheap SQL
    # pre-filter that also keeps the healthy majority out of the Python loop.
    dead = AuthToken.objects.select_related("character").filter(
        Q(revoked_at__isnull=False) | Q(refresh_fail_count__gte=_REVOKE_THRESHOLD)
    )
    for token in dead:
        ing = _corp_ingestion_scopes(token.scopes)
        if not ing:
            continue  # not a corp-ingestion token
        if _still_covered(token, ing):
            continue  # a live token still covers these scopes → superseded, not dead
        marker_key = f"sso:tokdeath:{token.id}"
        if AppSetting.objects.filter(key=marker_key).exists():
            continue  # already alerted this death
        if _emit_death(token, ing):
            AppSetting.objects.update_or_create(
                key=marker_key,
                defaults={"value": {
                    "at": timezone.now().isoformat(),
                    "fail": token.refresh_fail_count,
                    "revoked": token.revoked_at is not None,
                }},
            )
            alerted += 1

    cleared = _reconcile_markers()
    return {"status": "ok", "alerted": alerted, "cleared": cleared}
