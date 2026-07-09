"""SSO services: complete login, store tokens, verify membership, assign roles."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.corporation.models import EveCorporation
from apps.identity.models import Role, RoleAssignment
from core import rbac
from core.esi import oauth
from core.esi.client import ESIClient, ESIError
from core.mixins import Source

from .models import AuthToken, EveCharacter, EveScopeGrant
from .token_service import NoValidToken, get_valid_access_token

log = logging.getLogger("forca.sso")

# ESI scope + role string that mark a pilot as an in-game corporation Director.
ROLE_SCOPE = "esi-characters.read_corporation_roles.v1"
ESI_DIRECTOR_ROLE = "Director"

_ROLE_DEFAULTS = {
    rbac.ROLE_MEMBER: ("Member", rbac.ROLE_RANK[rbac.ROLE_MEMBER]),
    rbac.ROLE_OFFICER: ("Officer", rbac.ROLE_RANK[rbac.ROLE_OFFICER]),
    rbac.ROLE_DIRECTOR: ("Director", rbac.ROLE_RANK[rbac.ROLE_DIRECTOR]),
    rbac.ROLE_ADMIN: ("Admin", rbac.ROLE_RANK[rbac.ROLE_ADMIN]),
}


def ensure_role(key: str) -> Role:
    label, rank = _ROLE_DEFAULTS.get(key, (key.title(), 0))
    role, _ = Role.objects.get_or_create(key=key, defaults={"label": label, "rank": rank})
    return role


def store_token(character: EveCharacter, token: oauth.TokenResponse, scopes: list[str]) -> AuthToken:
    """Persist an OAuth token for a character (refresh token encrypted at rest)."""
    auth = AuthToken(
        character=character,
        scopes=scopes,
        token_type=token.token_type,
        access_expires_at=timezone.now() + timezone.timedelta(seconds=token.expires_in),
        last_refresh_ok_at=timezone.now(),
    )
    auth.refresh_token = token.refresh_token
    auth.access_token = token.access_token
    auth.save()
    # Every login/grant mints a NEW token; without pruning, a daily-active
    # pilot accumulates dozens of identical rows (44 on the health page once).
    # Older tokens whose scopes this one fully covers are now redundant —
    # revoke them. Tokens with EXTRA scopes (e.g. the director grant) survive.
    now = timezone.now()
    new_scopes = set(scopes)
    for old in AuthToken.objects.filter(
        character=character, revoked_at__isnull=True
    ).exclude(pk=auth.pk):
        if set(old.scopes or []) <= new_scopes:
            # Erase the ciphertext too (mirror disconnect_view) — a "revoked" row that
            # keeps its decryptable refresh token needlessly widens the surface a later
            # TOKEN_ENCRYPTION_KEY leak could decrypt for a superseded grant.
            old.revoked_at = now
            old._refresh_token = ""
            old._access_token = ""
            old.save(update_fields=["revoked_at", "_refresh_token", "_access_token"])
    for scope in scopes:
        EveScopeGrant.objects.update_or_create(
            character=character, scope=scope, defaults={"active": True}
        )
    return auth


class CharacterAlreadyLinked(Exception):
    """Raised when a character is already linked to a different account."""


class CharacterOwnershipChanged(Exception):
    """Raised when a character's EVE owner hash has changed since it was linked.

    EVE characters can be sold/transferred. When that happens the SSO ``owner``
    hash changes. We fail the login closed (rather than silently logging the new
    owner into the previous owner's account, inheriting their roles and private
    data) and require an officer/admin to detach the character first."""


def resolve_login_account(authenticated_user, character_id: int, name: str):
    """Find (or create) the platform account a logging-in character belongs to.

    Idempotent: a returning pilot whose character was disconnected or erased
    still has their ``eve:<id>`` account row, so we reuse it instead of trying to
    re-create it (which previously raised a duplicate-username 500 on re-login).
    """
    from django.contrib.auth import get_user_model

    if authenticated_user is not None and getattr(authenticated_user, "is_authenticated", False):
        return authenticated_user

    existing = (
        EveCharacter.objects.filter(character_id=character_id).select_related("user").first()
    )
    if existing and existing.user:
        return existing.user

    user_model = get_user_model()
    account, created = user_model.objects.get_or_create(
        username=f"eve:{character_id}",
        defaults={"first_name": (name or "")[:30]},
    )
    if created:
        account.set_unusable_password()
        account.save(update_fields=["password"])
    return account


def upsert_character(user, character_id: int, name: str, owner_hash: str = "") -> EveCharacter:
    existing = EveCharacter.objects.filter(character_id=character_id).first()
    if existing and existing.user_id and user and existing.user_id != user.id:
        # Never silently move a character (and its tokens) between accounts.
        raise CharacterAlreadyLinked(
            "This EVE character is already linked to another account."
        )
    # Ownership proof is only meaningful once a character is bound to an account:
    # an unlinked row is being claimed fresh, so we just record whatever owner hash
    # the claim carries. For an already-linked character we MUST prove the same
    # EVE owner before letting this login resolve into that account.
    if existing and existing.user_id and owner_hash:
        if existing.owner_hash and existing.owner_hash != owner_hash:
            # Transferred to a different EVE account since we last saw it.
            raise CharacterOwnershipChanged(
                "This EVE character's ownership has changed; contact an officer to re-link it."
            )
        if not existing.owner_hash:
            # Legacy row that predates owner-hash capture (or whose hash was never
            # recorded): we cannot prove the logger-in is the original owner, so we
            # fail closed rather than risk handing a transferred character the prior
            # owner's account. An officer detach turns it back into a fresh claim.
            raise CharacterOwnershipChanged(
                "This EVE character cannot be verified; contact an officer to re-link it."
            )
    defaults = {"name": name, "source": Source.ESI_CHAR}
    if owner_hash:
        defaults["owner_hash"] = owner_hash
    character, _ = EveCharacter.objects.update_or_create(
        character_id=character_id,
        defaults=defaults,
    )
    if character.user_id is None and user:
        character.user = user
        character.save(update_fields=["user"])
    if user and not user.characters.filter(is_main=True).exists():
        character.is_main = True
        character.save(update_fields=["is_main"])
        user.main_character_id = character.character_id
        user.save(update_fields=["main_character_id"])
    return character


def detach_character(character: EveCharacter, *, actor, reason: str = "") -> dict:
    """Officer recovery: unlink a character and clear its owner hash so the real owner
    can re-claim it at the next SSO login.

    Undoes exactly what the fail-closed login guards protect against: a character that
    was sold (owner hash changed) or predates owner-hash capture is stuck with the
    on-screen advice to "contact an officer to detach it", but no such flow existed.
    This clears ``owner_hash`` (so the next login records a fresh one) and ``user``
    (so ``upsert_character`` treats it as a fresh claim), **revokes the character's
    tokens** (they authorised the prior link and must not survive it), re-reconciles
    the prior owner's auto-roles, and clears that account's main pointer if it named
    this character. Fully audited. The caller enforces officer gating + the guard
    that an officer cannot detach a Director/Admin-linked character.
    """
    from django.utils import timezone

    from core.audit import audit_log

    from .models import AuthToken, EveScopeGrant

    now = timezone.now()
    prior_user = character.user
    prior_user_id = prior_user.id if prior_user else None
    # Revoke the character's tokens AND erase their ciphertext — they authorised the
    # prior link and must not survive it (revocation is honoured by every consumer;
    # wiping the ciphertext matches the disconnect/erasure hardening).
    revoked = AuthToken.objects.filter(character=character, revoked_at__isnull=True).update(
        revoked_at=now, _refresh_token="", _access_token=""
    )
    # Scope grants are keyed to the character, not the user; a re-claim by a new owner
    # must not inherit the prior owner's feature grants, so clear them (as GDPR erasure does).
    EveScopeGrant.objects.filter(character=character).delete()
    character.user = None
    character.owner_hash = ""
    character.is_main = False
    character.save(update_fields=["user", "owner_hash", "is_main"])
    if prior_user is not None:
        # If the detached character *defined* the account username (``eve:<id>``),
        # retire that username so a re-claim mints a FRESH account instead of resolving
        # the new owner back into the seller's account (and its manual roles/history).
        if prior_user.username == f"eve:{character.character_id}":
            prior_user.username = f"eve:{character.character_id}:detached:{int(now.timestamp())}"
            prior_user.save(update_fields=["username"])
        if getattr(prior_user, "main_character_id", None) == character.character_id:
            prior_user.main_character_id = None
            prior_user.save(update_fields=["main_character_id"])
        # Membership/Director follow in-game reality for the account that lost the char.
        sync_roles_for_user(prior_user)
    audit_log(
        actor, "sso.character_detached",
        target_type="eve_character", target_id=str(character.character_id),
        metadata={
            "reason": reason, "prior_user_id": prior_user_id,
            "character_name": character.name, "tokens_revoked": revoked,
        },
    )
    return {"prior_user_id": prior_user_id, "tokens_revoked": revoked}


def _set_corp_alliance(corp: EveCorporation, alliance_id: int | None) -> None:
    """Record a corporation's alliance (or clear it), keeping the FK consistent."""
    from apps.corporation.models import EveAlliance

    new_id = alliance_id or None
    if corp.alliance_id == new_id:
        return
    if new_id is not None:
        EveAlliance.objects.get_or_create(alliance_id=new_id)
    corp.alliance_id = new_id
    corp.save(update_fields=["alliance"])


def _apply_affiliation(
    character: EveCharacter, corp_id: int | None, alliance_id: int | None
) -> None:
    """Record a character's current corp/alliance + home-corp membership.

    Shared by the single-character login refresh and the batched sweep so both
    stay byte-for-byte consistent on the membership/alliance-FK bookkeeping."""
    if corp_id:
        corp, _ = EveCorporation.objects.get_or_create(
            corporation_id=corp_id,
            defaults={"is_home_corp": corp_id == settings.FORCA_HOME_CORP_ID},
        )
        # A character's alliance is its corporation's alliance — record it on the
        # corp so the home corp's alliance is known (drives alliance-service
        # access). Keeping it current here means a corp that changes alliance, or
        # leaves one, is reflected on the next affiliation refresh.
        _set_corp_alliance(corp, alliance_id)
        character.corporation = corp
    character.alliance_id = alliance_id
    character.is_corp_member = bool(corp_id) and corp_id == settings.FORCA_HOME_CORP_ID
    character.affiliation_updated_at = timezone.now()
    character.save(
        update_fields=["corporation", "alliance_id", "is_corp_member", "affiliation_updated_at"]
    )


def refresh_affiliation(character: EveCharacter, client: ESIClient | None = None) -> None:
    """Fetch public character info to learn current corp/alliance and set
    membership. Best-effort: failures leave prior values intact."""
    client = client or ESIClient()
    try:
        resp = client.get(f"/characters/{character.character_id}/", essential=True)
    except ESIError as exc:
        log.warning("affiliation refresh failed for %s: %s", character.character_id, exc)
        return
    data = resp.data or {}
    _apply_affiliation(character, data.get("corporation_id"), data.get("alliance_id"))


def refresh_affiliations_batched(
    *, staleness_hours: int = 4, limit: int | None = None, client: ESIClient | None = None
) -> dict:
    """Refresh every stale character's affiliation via the **batched** ESI endpoint.

    Retires the audit's single-point-of-failure at alt scale: the old sweep called
    ``GET /characters/{id}/`` once *per character* every 6h and re-ran the full
    Director-role check once *per character* — an ESI fan-out that grows linearly
    with the corp's alt count and can burn the shared error budget (a 420/ban risks
    ALL data flow). This uses ``POST /characters/affiliation/`` (up to 1000 ids per
    call) with a staleness cutoff so a just-refreshed character (e.g. at login) is
    skipped, and reconciles the **DB-only member role once per affected user**.

    The ESI-heavy in-game Director-role re-check is deliberately **not** run here — it
    lives in its own staleness-filtered, per-run-capped task
    (``reconcile_director_roles``) so its cost stays bounded and decoupled from alt
    count. Running it inline (the roles scope is a default grant, so nothing
    short-circuits) would re-introduce an O(member-count) ESI GET fan-out every sweep —
    the very thing this feature exists to retire.

    Best-effort per batch: a failed chunk is logged and skipped, leaving prior
    values intact; one bad user's role sync never aborts the sweep.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import F, Q

    cutoff = timezone.now() - timezone.timedelta(hours=staleness_hours)
    qs = EveCharacter.objects.filter(
        Q(affiliation_updated_at__isnull=True) | Q(affiliation_updated_at__lt=cutoff)
    ).order_by(F("affiliation_updated_at").asc(nulls_first=True))
    if limit:
        qs = qs[:limit]
    characters = list(qs)
    if not characters:
        return {"checked": 0, "batches": 0, "updated": 0, "users_synced": 0}

    by_id = {c.character_id: c for c in characters}
    client = client or ESIClient()
    ids = list(by_id.keys())
    affected_user_ids: set[int] = set()
    batches = updated = 0
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        try:
            resp = client.post("/characters/affiliation/", json=chunk, essential=True)
        except ESIError as exc:  # ESIRateLimited is a subclass — both leave prior values intact
            log.warning("batched affiliation failed for %d ids: %s", len(chunk), exc)
            continue
        batches += 1
        # ESI returns a JSON array on 200; guard against an unexpected shape so a
        # single odd response can't abort the whole sweep.
        rows = resp.data if isinstance(resp.data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            character = by_id.get(row.get("character_id"))
            if character is None:
                continue
            _apply_affiliation(character, row.get("corporation_id"), row.get("alliance_id"))
            updated += 1
            if character.user_id:
                affected_user_ids.add(character.user_id)

    # Reconcile the DB-only MEMBER role ONCE per affected user (the old loop did it per
    # character). check_director=False keeps this sweep ESI-free; Director is handled by
    # the dedicated bounded task. Membership follows the affiliation we just wrote, so a
    # pilot who left the home corp loses member (and Director, via that task) correctly.
    user_model = get_user_model()
    for user in user_model.objects.filter(id__in=affected_user_ids):
        try:
            sync_roles_for_user(user, check_director=False)
        except Exception:  # noqa: BLE001 - one user's role sync must not abort the sweep
            log.exception("role sync failed for user %s during affiliation sweep", user.pk)

    return {
        "checked": len(characters),
        "batches": batches,
        "updated": updated,
        "users_synced": len(affected_user_ids),
    }


def reconcile_director_roles(*, staleness_hours: int = 5, limit: int = 200) -> dict:
    """Periodic, bounded in-game Director-role reconcile (4.8).

    Decoupled from the affiliation sweep so its ESI cost (the Director check does 1
    non-essential ``/roles/`` GET + 1 co-check per home-corp member with the — default —
    roles scope) is **staleness-filtered + per-run capped** instead of firing for every
    member every sweep. Picks the ``limit`` stalest home-corp member characters (never
    checked, or checked > ``staleness_hours`` ago), reconciles each owning user's roles
    once with the full Director check, and stamps the checked characters so they rotate
    to the back — preserving Director grant/withdrawal while bounding the fan-out.

    With the default 5h staleness under a 6h beat, a corp of <=``limit`` member chars is
    fully re-checked every run (same cadence as before); a larger corp rotates through
    over successive runs so no single run's ESI cost scales with total membership.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import F, Q

    cutoff = timezone.now() - timezone.timedelta(hours=staleness_hours)
    rows = list(
        EveCharacter.objects.filter(is_corp_member=True)
        .filter(Q(director_checked_at__isnull=True) | Q(director_checked_at__lt=cutoff))
        .order_by(F("director_checked_at").asc(nulls_first=True))
        .values_list("character_id", "user_id")[:limit]
    )
    if not rows:
        return {"checked": 0, "users_synced": 0}
    checked_ids = [cid for cid, _uid in rows]
    user_ids = {uid for _cid, uid in rows if uid}

    user_model = get_user_model()
    for user in user_model.objects.filter(id__in=user_ids):
        try:
            sync_roles_for_user(user, check_director=True)
        except Exception:  # noqa: BLE001 - one user's check must not abort the reconcile
            log.exception("director reconcile failed for user %s", user.pk)
    # Stamp every selected char (even tokenless ones) so it rotates to the back and the
    # per-run set stays bounded regardless of how many are re-checked.
    EveCharacter.objects.filter(character_id__in=checked_ids).update(
        director_checked_at=timezone.now()
    )
    return {"checked": len(checked_ids), "users_synced": len(user_ids)}


def character_is_corp_director(
    character: EveCharacter, client: ESIClient | None = None
) -> bool | None:
    """Whether a character holds the in-game corporation Director role.

    Returns True/False when ESI answers, or None when we can't tell (no token,
    the roles scope was not granted, or ESI failed) — callers must treat None as
    "unknown" and never act on it, so a missing scope never strips a role.
    """
    try:
        access = get_valid_access_token(character, [ROLE_SCOPE])
    except NoValidToken:
        return None
    client = client or ESIClient()
    # Co-verify the character is CURRENTLY in the home corp from live public affiliation
    # at the same moment we read roles. `/roles/` names no corporation, so without this a
    # Directorship held in a *different* corp would be mistaken for a home-corp
    # Directorship whenever the cached `is_corp_member` flag is stale (refresh_affiliation
    # fails open on an ESI error). An unreadable affiliation returns None ("unknown"), so
    # the caller leaves the role unchanged rather than acting on incomplete data.
    try:
        pub = client.get(f"/characters/{character.character_id}/", essential=True).data or {}
    except ESIError as exc:
        log.warning("director co-check affiliation failed for %s: %s", character.character_id, exc)
        return None
    if pub.get("corporation_id") != settings.FORCA_HOME_CORP_ID:
        return False
    try:
        resp = client.get(f"/characters/{character.character_id}/roles/", token=access)
    except ESIError as exc:
        log.warning("corp-roles lookup failed for %s: %s", character.character_id, exc)
        return None
    return ESI_DIRECTOR_ROLE in (resp.data or {}).get("roles", [])


def sync_roles_for_user(user, *, check_director: bool = True) -> None:
    """Reconcile auto-managed roles, then propagate access to external comms (best-effort).

    The role reconcile is delegated to ``_reconcile_roles_for_user``; afterwards a comms
    access reconcile is enqueued so a membership/role change — notably leaving the corp or
    disconnecting the proving token — withdraws the pilot's Discord/Slack/Mumble roles in
    near-real-time instead of waiting for the periodic sweep. The enqueue is a guarded no-op
    unless ``COMMS_ACCESS_ENABLED`` (see ``apps.comms_access.hooks``), so the login hot path
    that calls this with ``check_director=False`` pays only an attribute lookup.
    """
    _reconcile_roles_for_user(user, check_director=check_director)
    from apps.comms_access.hooks import enqueue_user_reconcile

    enqueue_user_reconcile(user, source_ref="role-sync")


def _reconcile_roles_for_user(user, *, check_director: bool = True) -> None:
    """Reconcile auto-managed roles for a user.

    `member` follows home-corp membership. `director` follows the in-game
    corporation Director role: a pilot who is a Director is granted the app's
    Director role at login automatically (no manual step), and it is withdrawn
    once we confirm none of their corp characters are Directors any more. When we
    cannot read roles (scope not yet granted / ESI down) the Director role is left
    exactly as-is. Officer and admin remain manual.

    ``check_director=False`` reconciles only the (DB-only, instant) member role and
    leaves the Director grant untouched — used on the login critical path, where the
    Director ESI check (2 calls per director-scoped character) is deferred to the
    ``warm_pilot_after_login`` task so it never slows the redirect to the dashboard.
    """
    member_chars = list(user.characters.filter(is_corp_member=True))
    member_role = ensure_role(rbac.ROLE_MEMBER)
    director_role = ensure_role(rbac.ROLE_DIRECTOR)
    if not member_chars:
        # Not in the home corp at all → can be neither member nor Director.
        RoleAssignment.objects.filter(
            user=user, role__in=[member_role, director_role]
        ).delete()
        return

    RoleAssignment.objects.get_or_create(user=user, role=member_role)

    if not check_director:
        return

    # Director must be a member of the home corp; only those characters can carry
    # the Director role, so they're the only ones worth querying.
    statuses = [character_is_corp_director(c) for c in member_chars]
    if any(s is True for s in statuses):
        RoleAssignment.objects.get_or_create(user=user, role=director_role)
    elif any(s is False for s in statuses):
        # ESI affirmatively says no corp character is a Director.
        RoleAssignment.objects.filter(user=user, role=director_role).delete()
    else:
        # Every status is "unknown". Distinguish "no token can ever prove it"
        # (the proving token was revoked/disconnected/deleted → withdraw, so a
        # stale Director grant can't outlive the token that justified it) from a
        # transient ESI failure while a proving token still exists (keep, to avoid
        # flapping the role off every time ESI is briefly unreachable).
        proving_tokens = AuthToken.objects.filter(
            character__in=member_chars, revoked_at__isnull=True
        )
        has_proving_token = any(ROLE_SCOPE in (t.scopes or []) for t in proving_tokens)
        if not has_proving_token:
            RoleAssignment.objects.filter(user=user, role=director_role).delete()


def complete_login(user, claims: dict, token: oauth.TokenResponse) -> EveCharacter:
    """Link a character after a validated SSO callback, store its token,
    refresh affiliation, and (re)assign roles."""
    character_id = oauth.character_id_from_claims(claims)
    name = claims.get("name", "")
    owner_hash = oauth.owner_hash_from_claims(claims)
    scopes = oauth.scopes_from_claims(claims)

    character = upsert_character(user, character_id, name, owner_hash)
    store_token(character, token, scopes)
    refresh_affiliation(character)
    # Assign the member role now (DB-only, needed for the membership gate on the first
    # page), but defer the Director ESI check + the cold Command-Center cache warm to a
    # background task so the redirect to /dashboard/ isn't blocked on ESI + a multi-second
    # readiness recompute. Best-effort: a broker hiccup must never fail the login.
    sync_roles_for_user(user, check_director=False)
    try:
        from .tasks import warm_pilot_after_login

        warm_pilot_after_login.delay(character_id)
    except Exception:  # noqa: BLE001 - login must succeed even if the task can't be queued
        log.warning("could not enqueue warm_pilot_after_login for %s", character_id)
    return character
