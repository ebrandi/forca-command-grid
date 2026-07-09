"""Pilot eligibility — the single enforcement point for the enrolment rule.

A pilot may earn tickets / win prizes **only** when, per the contest's policy:

1. they have a FORCA account (an ``EveCharacter`` linked to a ``User``);
2. they authenticated through the app (that link exists);
3. they hold a valid, non-revoked ESI token;
4. that token carries the contest's required scopes;
5. they are a recognised corp pilot (``is_corp_member``) — or, if the contest opts
   in, a registered alliance / friendly-corp pilot;
6. they are not manually excluded.

Every check here is **DB-only and request-safe** — it never calls ESI. A live
token refresh is a Celery-only network op (``token_service.get_valid_access_token``);
eligibility instead reads the persisted token state, which the affiliation /
prune beats keep current. The result is a small explainable object the ticket
engine branches on and the dashboard renders into calls-to-action.

Eligibility is an **account-level** property: an account is eligible if *any* of
its characters is. The heavy read paths (leaderboard recompute, draw census) use
:func:`for_users_bulk`, which prefetches tokens and preloads exclusions once so
there is no per-pilot query.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.sso.models import EveCharacter

# ESI-status labels (mirror RaffleTicketLedgerEntry.EsiStatus / RaffleIneligibleActivity.Reason).
ESI_VALID = "valid"
ESI_EXPIRED = "expired"
ESI_REVOKED = "revoked"
ESI_NONE = "none"

# Number of consecutive refresh failures after which we treat a non-revoked token
# as effectively expired (CCP has stopped honouring the refresh token).
_REFRESH_FAIL_LIMIT = 5


@dataclass
class _Ctx:
    """Preloaded context for bulk eligibility — avoids per-pilot queries."""

    excluded_user_ids: set[int] = field(default_factory=set)
    excluded_char_ids: set[int] = field(default_factory=set)
    include_alliance: bool = False
    alliance_ids: set[int] = field(default_factory=set)
    corp_ids: set[int] = field(default_factory=set)


@dataclass
class Eligibility:
    """The explainable outcome of an eligibility check for one character/account."""

    eligible: bool = False
    enrolled: bool = False
    has_valid_token: bool = False
    scopes_ok: bool = True
    is_corp_member: bool = False
    excluded: bool = False
    esi_status: str = ESI_NONE
    reason_code: str = ""          # machine code (maps to RaffleIneligibleActivity.Reason)
    message: str = ""              # short human explanation
    cta: str = ""                  # what the pilot should do next
    user_id: int | None = None
    character_id: int | None = None
    character_name: str = ""
    missing_scopes: list[str] = field(default_factory=list)
    eligible_since: object = None  # datetime the account first had a valid token

    def snapshot(self) -> dict:
        """Compact JSON stored on the ledger entry / ineligible row for audit."""
        return {
            "enrolled": self.enrolled,
            "valid_token": self.has_valid_token,
            "scopes_ok": self.scopes_ok,
            "corp_member": self.is_corp_member,
            "excluded": self.excluded,
            "esi_status": self.esi_status,
        }


def _token_state(character: EveCharacter):
    """(has_live_token, esi_status, union_scopes, earliest_live_created_at).

    Uses only persisted columns / prefetched relations — no network. A character
    can own several tokens; the union of scopes over *non-revoked* tokens is what
    the pilot effectively holds, and the earliest live token's creation time is
    when they connected ESI (the "eligible since" for this character).
    """
    tokens = list(character.tokens.all())
    if not tokens:
        return False, ESI_NONE, set(), None
    live = [t for t in tokens if t.revoked_at is None and t._refresh_token]
    if not live:
        return False, ESI_REVOKED, set(), None
    healthy = [t for t in live if t.refresh_fail_count < _REFRESH_FAIL_LIMIT]
    scopes: set[str] = set()
    for t in (healthy or live):
        scopes.update(t.scopes or [])
    if not healthy:
        return False, ESI_EXPIRED, scopes, None
    since = min((t.created_at for t in healthy if t.created_at), default=None)
    return True, ESI_VALID, scopes, since


def _is_excluded(contest, user, character_id, ctx: _Ctx | None):
    if ctx is not None:
        return (user is not None and user.id in ctx.excluded_user_ids) or (
            character_id is not None and character_id in ctx.excluded_char_ids
        )
    from django.db.models import Q

    from .models import RaffleExclusion

    q = RaffleExclusion.objects.filter(contest=contest, active=True)
    if user is not None and character_id is not None:
        return q.filter(Q(user=user) | Q(character_id=character_id)).exists()
    if user is not None:
        return q.filter(user=user).exists()
    if character_id is not None:
        return q.filter(character_id=character_id).exists()
    return False


def _corp_or_alliance(character: EveCharacter, user, contest, ctx: _Ctx | None) -> bool:
    if character.is_corp_member:
        return True
    if not contest.include_alliance or user is None:
        return False
    if ctx is not None:
        # In-memory check over the account's (prefetched) characters.
        for ch in user.characters.all():
            if ch.corporation_id and ch.corporation_id in ctx.corp_ids:
                return True
            if ch.alliance_id and ch.alliance_id in ctx.alliance_ids:
                return True
        return False
    # Import-local + never memoised: a revoked partner must lose access live.
    from apps.corporation.access import is_service_alliance_pilot

    return is_service_alliance_pilot(user)


def for_character_id(contest, character_id: int, *, character_name: str = "") -> Eligibility:
    """Resolve a raw EVE ``character_id`` (e.g. off a killmail) to an eligibility."""
    character = (
        EveCharacter.objects.filter(character_id=character_id)
        .select_related("user")
        .first()
    )
    if character is None:
        return Eligibility(
            reason_code="not_enrolled", esi_status=ESI_NONE,
            character_id=character_id, character_name=character_name,
            message="This pilot has no FORCA Command Grid account.",
            cta="Ask them to sign in with EVE SSO to start earning tickets.",
        )
    return for_character(contest, character, character_name=character_name or character.name)


def for_character(contest, character: EveCharacter, *, character_name: str = "",
                  ctx: _Ctx | None = None) -> Eligibility:
    """Eligibility for a known ``EveCharacter`` under a contest's policy."""
    name = character_name or character.name or str(character.character_id)
    user = character.user
    result = Eligibility(
        character_id=character.character_id,
        character_name=name,
        user_id=user.id if user else None,
        enrolled=user is not None,
    )

    if user is None:
        result.reason_code = "not_enrolled"
        result.esi_status = ESI_NONE
        result.message = "This character isn't linked to a FORCA account."
        result.cta = "Sign in with EVE SSO to enrol and start earning tickets."
        return result

    has_token, esi_status, scopes, _since = _token_state(character)
    result.has_valid_token = has_token
    result.esi_status = esi_status
    # "Eligible since" = when this character first appeared in FORCA (first login /
    # enrolment). Used only by the non-retroactive gate to refuse tickets for
    # activity that predates enrolment. added_at is a real, settable enrolment
    # timestamp (the token's created_at is auto-managed and unreliable for this).
    result.eligible_since = character.added_at

    required = set(contest.required_scopes or [])
    result.missing_scopes = sorted(required - scopes)
    result.scopes_ok = not result.missing_scopes

    result.is_corp_member = _corp_or_alliance(character, user, contest, ctx)
    result.excluded = _is_excluded(contest, user, character.character_id, ctx)

    if result.excluded:
        result.reason_code = "excluded"
        result.message = "You've been excluded from this contest by leadership."
        result.cta = "Contact a director if you believe this is a mistake."
        return result
    if contest.require_valid_token and not has_token:
        result.reason_code = "no_token" if esi_status == ESI_NONE else "token_expired"
        if esi_status == ESI_NONE:
            result.message = "You haven't connected an ESI token yet."
            result.cta = "Connect your ESI token to start earning raffle tickets."
        else:
            result.message = "Your ESI token has expired or been revoked."
            result.cta = "Reconnect your ESI token to keep earning tickets."
        return result
    if not result.scopes_ok:
        result.reason_code = "missing_scope"
        result.message = "Your ESI token is missing a scope this contest needs."
        result.cta = "Re-authorise with the required scopes to earn tickets."
        return result
    if not result.is_corp_member:
        result.reason_code = "not_corp"
        result.message = "Only recognised corporation pilots can take part."
        result.cta = "Make sure your main is in the corp and re-sync your affiliation."
        return result

    result.eligible = True
    result.message = "You're eligible — fly, earn tickets and check your progress."
    return result


def for_user(contest, user, *, ctx: _Ctx | None = None) -> Eligibility:
    """Eligibility for a logged-in account (eligible if ANY character is).

    Tickets from all of a pilot's characters aggregate to the account, so the
    account is eligible as soon as one character passes. Returns the eligible
    character's result, else the "closest to eligible" one for a helpful CTA.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return Eligibility(
            reason_code="not_enrolled",
            message="Sign in with EVE SSO to take part in the raffle.",
            cta="Sign in to enrol and start earning tickets.",
        )
    characters = list(user.characters.all())
    if not characters:
        return Eligibility(
            enrolled=True, user_id=user.id, reason_code="no_token", esi_status=ESI_NONE,
            message="Your account has no linked EVE character yet.",
            cta="Link a character with EVE SSO to earn tickets.",
        )
    best: Eligibility | None = None
    for character in characters:
        elig = for_character(contest, character, ctx=ctx)
        if elig.eligible:
            return elig
        if best is None or _rank(elig) > _rank(best):
            best = elig
    return best or Eligibility()


# --------------------------------------------------------------------------- #
#  Bulk path (leaderboard recompute + draw census) — no per-pilot query
# --------------------------------------------------------------------------- #
def build_context(contest) -> _Ctx:
    """Preload the exclusion + service-id sets a contest needs, once."""
    from .models import RaffleExclusion

    ctx = _Ctx(include_alliance=bool(contest.include_alliance))
    for user_id, char_id in RaffleExclusion.objects.filter(
        contest=contest, active=True
    ).values_list("user_id", "character_id"):
        if user_id:
            ctx.excluded_user_ids.add(user_id)
        if char_id:
            ctx.excluded_char_ids.add(char_id)
    if ctx.include_alliance:
        from apps.corporation.access import service_alliance_ids, service_corp_ids

        ctx.alliance_ids = set(service_alliance_ids())
        ctx.corp_ids = set(service_corp_ids())
    return ctx


def for_users_bulk(contest, user_ids) -> dict[int, Eligibility]:
    """``{user_id: Eligibility}`` for many accounts with prefetched tokens.

    One query for the users (with characters + tokens prefetched) + one for the
    exclusions — then all evaluation is in memory. Used by the 15-min leaderboard
    recompute and the draw census so neither does an N+1.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Prefetch

    from apps.sso.models import AuthToken

    ids = [u for u in user_ids if u is not None]
    if not ids:
        return {}
    ctx = build_context(contest)
    users = (
        get_user_model().objects.filter(pk__in=ids)
        .prefetch_related(
            Prefetch(
                "characters",
                queryset=EveCharacter.objects.prefetch_related(
                    Prefetch("tokens", queryset=AuthToken.objects.all())
                ),
            )
        )
    )
    return {u.pk: for_user(contest, u, ctx=ctx) for u in users}


def _rank(e: Eligibility) -> int:
    """How close to eligible a result is (higher = closer) — for picking the best CTA."""
    return (
        int(e.enrolled) + int(e.has_valid_token) + int(e.scopes_ok) + int(e.is_corp_member)
        - (10 if e.excluded else 0)
    )
