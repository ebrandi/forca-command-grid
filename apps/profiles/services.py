"""Public profile domain logic: publish / update / rotate / retire, and the public resolver.

Two audiences, two halves:

* The **owner half** (``publish``, ``rotate``, ``set_enabled``, ``retire``) is called from the
  management page with a character the view has ALREADY resolved through the caller's own
  account (``core.pilots.owned_pilot``) — the IDOR-safe shape the rest of the pilot surface
  uses. Every mutation is audited; the audit row never carries the token or the password.
* The **visitor half** (``resolve_public``, ``visitor_context``) serves the anonymous page.
  ``resolve_public`` is the single chokepoint that decides whether a token is served at all,
  and it fails closed on every axis: token format, row present, enabled, not expired, character
  still linked to the publishing account, that account still a corp member. A token that fails
  any of these is indistinguishable from one that never existed (the view answers the same 404).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.core.cache import cache
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from core import rbac
from core.audit import audit_log

from .models import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, TOKEN_RE, PublicProfile
from .sections import clean_sections, renderable_sections

# --------------------------------------------------------------------------- #
#  Expiry presets (the management form) — value → days; "never" and "custom" are special.
# --------------------------------------------------------------------------- #
EXPIRY_PRESETS: list[tuple[str, int | None]] = [
    ("7d", 7),
    ("30d", 30),
    ("90d", 90),
    ("never", None),
    ("custom", None),
]
EXPIRY_KEYS = {k for k, _days in EXPIRY_PRESETS}
DEFAULT_EXPIRY = "30d"
# A custom date may not sit more than this far out — "never" exists for that.
MAX_CUSTOM_DAYS = 3650


class ProfileError(ValueError):
    """A user-facing validation failure (message already translated)."""


def expiry_from_form(choice: str, custom_date: str) -> dt.datetime | None:
    """Turn the form's preset + optional date into an aware ``expires_at`` (or ``None``).

    A preset counts whole days from now. A custom date is a calendar day (``YYYY-MM-DD``) and
    expires at the END of that day, UTC — a pilot who writes "the 15th" means the link works
    on the 15th. Past dates and absurdly distant ones are refused.
    """
    choice = (choice or DEFAULT_EXPIRY).strip()
    if choice not in EXPIRY_KEYS:
        raise ProfileError(_("Choose how long the link should stay live."))
    if choice == "never":
        return None
    if choice != "custom":
        days = dict(EXPIRY_PRESETS)[choice]
        return timezone.now() + dt.timedelta(days=days)
    try:
        day = dt.date.fromisoformat((custom_date or "").strip())
    except ValueError:
        raise ProfileError(_("Enter the expiry date as YYYY-MM-DD.")) from None
    end_of_day = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.UTC)
    now = timezone.now()
    if end_of_day <= now:
        raise ProfileError(_("The expiry date has to be in the future."))
    if end_of_day > now + dt.timedelta(days=MAX_CUSTOM_DAYS):
        raise ProfileError(_("Pick a date within the next ten years, or choose “never”."))
    return end_of_day


def validate_sections(keys) -> list[str]:
    """The submitted section keys, normalised — at least one, or the page would be a bare
    name card, which is never what a pilot meant by "share my profile"."""
    sections = clean_sections(keys)
    if not sections:
        raise ProfileError(_("Pick at least one thing to show on the page."))
    return sections


def validate_password(raw: str) -> str:
    raw = raw or ""
    if len(raw) < PASSWORD_MIN_LENGTH:
        raise ProfileError(
            _("The password needs at least %(n)d characters.") % {"n": PASSWORD_MIN_LENGTH}
        )
    if len(raw) > PASSWORD_MAX_LENGTH:
        raise ProfileError(
            _("The password can be at most %(n)d characters.") % {"n": PASSWORD_MAX_LENGTH}
        )
    return raw


# --------------------------------------------------------------------------- #
#  Owner half.
# --------------------------------------------------------------------------- #
def profiles_for(user) -> dict[int, PublicProfile]:
    """``{character_id: profile}`` for every profile this account has published."""
    return {
        p.character_id: p
        for p in PublicProfile.objects.filter(owner=user).select_related("character")
    }


@dataclass
class PublishInput:
    sections: list[str]
    expires_at: dt.datetime | None
    note: str
    # ``None`` = leave the password as it is; ``""`` = clear it; anything else = set it.
    password: str | None = None


def publish(user, character, data: PublishInput, *, ip: str = "") -> tuple[PublicProfile, bool]:
    """Create the character's profile, or update it in place. Returns ``(profile, created)``.

    An update keeps the token (the pilot's already-shared link keeps working) — rotating the
    link is a separate, explicit action. The password is only touched when the caller says so.
    """
    sections = validate_sections(data.sections)
    note = (data.note or "").strip()[: PublicProfile._meta.get_field("note").max_length]
    profile, created = PublicProfile.objects.get_or_create(
        character=character,
        defaults={"owner": user, "sections": sections, "expires_at": data.expires_at,
                  "note": note},
    )
    changed: list[str] = []
    if not created:
        if profile.owner_id != user.pk:
            # The character was re-linked to a different account since the row was made. The
            # new account is the pilot's current human; the old publication is theirs no longer.
            profile.owner = user
            changed.append("owner")
        profile.sections = sections
        profile.expires_at = data.expires_at
        profile.note = note
        changed += ["sections", "expires_at", "note"]
    if data.password is not None:
        profile.set_password(data.password or None)
        changed.append("password_hash")
    if not created:
        # ``enabled`` is deliberately NOT touched: a pilot who disabled the page and then edits
        # its settings gets a disabled page with new settings. Re-enabling is its own button.
        profile.save(update_fields=[*changed, "updated_at"])
    elif data.password is not None:
        profile.save(update_fields=["password_hash", "updated_at"])
    audit_log(
        user,
        "profile.published" if created else "profile.updated",
        target_type="character",
        target_id=str(character.character_id),
        metadata={
            "sections": sections,
            "expires_at": data.expires_at.isoformat() if data.expires_at else None,
            "password": profile.has_password,
        },
        ip=ip,
    )
    return profile, created


def rotate(profile: PublicProfile, user, *, ip: str = "") -> str:
    """Replace the link. The old URL 404s immediately. Visitor password grants are keyed by
    profile, not token, so they survive — which is harmless: a grant opens nothing without the
    new link. Setting a new password (through :func:`publish`) is what revokes the people
    already let in."""
    token = profile.rotate_token()
    profile.save(update_fields=["token", "updated_at"])
    audit_log(user, "profile.rotated", target_type="character",
              target_id=str(profile.character_id), ip=ip)
    return token


def set_enabled(profile: PublicProfile, user, enabled: bool, *, ip: str = "") -> None:
    profile.enabled = enabled
    profile.save(update_fields=["enabled", "updated_at"])
    audit_log(user, "profile.enabled" if enabled else "profile.disabled",
              target_type="character", target_id=str(profile.character_id), ip=ip)


def retire(profile: PublicProfile, user, *, ip: str = "") -> None:
    """Delete the profile outright (token, password, settings and view counters)."""
    character_id = profile.character_id
    profile.delete()
    audit_log(user, "profile.deleted", target_type="character", target_id=str(character_id),
              ip=ip)


# --------------------------------------------------------------------------- #
#  Visitor half.
# --------------------------------------------------------------------------- #
def resolve_public(token: str) -> PublicProfile | None:
    """The profile a public token resolves to, or ``None`` — the ONLY way the public view finds
    a row. Every axis fails closed (see the module docstring)."""
    if not token or not TOKEN_RE.match(token):
        return None
    profile = (
        PublicProfile.objects.filter(token=token)
        .select_related("character", "character__corporation", "owner")
        .first()
    )
    if profile is None or not profile.is_live:
        return None
    character = profile.character
    # Still the same human's pilot? An unlinked character (user=None) or one re-linked to
    # another account must never be served under a link the previous holder published.
    if character.user_id is None or character.user_id != profile.owner_id:
        return None
    # The corp hosts this page for its members. An account that has left (or been removed)
    # takes its publications down with it. Account-wide authority is the right question here:
    # no request pilot is attached to a *visitor's* view of someone else's account.
    if not rbac.has_role(profile.owner, rbac.ROLE_MEMBER):
        return None
    return profile


def record_view(profile: PublicProfile) -> None:
    """Count a visitor render (a single UPDATE, never a read-modify-write)."""
    PublicProfile.objects.filter(pk=profile.pk).update(
        view_count=F("view_count") + 1, last_viewed_at=timezone.now()
    )


def visitor_context(profile: PublicProfile) -> dict:
    """Everything the public template renders for ``profile``, built ONLY from the sections the
    pilot chose. Nothing about the owner's other characters is ever loaded here."""
    from apps.skills.overview import character_training, grouped_skills

    character = profile.character
    sections = renderable_sections(profile.sections)
    ctx: dict = {
        "profile": profile,
        "character": character,
        "sections": sections,
        "note": profile.note,
    }
    snapshot = None
    if "skills" in sections or "ships" in sections:
        snapshot = character.skill_snapshots.filter(is_latest=True).first()
    if "skills" in sections:
        groups = grouped_skills(snapshot)
        ctx["skills"] = {
            "snapshot": snapshot,
            "groups": groups,
            "total_sp": snapshot.total_sp if snapshot else 0,
            "count": len(snapshot.skills) if snapshot else 0,
            "at_v": sum(g["at_v"] for g in groups),
            "as_of": snapshot.as_of if snapshot else None,
        }
    if "queue" in sections:
        ctx["training"] = character_training(character)
    if "ships" in sections:
        ctx["ships"] = _flyable_cached(snapshot)
    if "attributes" in sections:
        ctx["attributes"] = _attributes(character)
    if "combat" in sections:
        ctx["combat"] = _combat(character)
    return ctx


def _flyable_cached(snapshot) -> list[dict]:
    """Flyable hulls, memoised per snapshot row (the SDE requirement scan is a few thousand
    rows — cheap, but not per anonymous hit)."""
    from apps.skills.flyable import flyable_ships

    if snapshot is None:
        return []
    key = f"profiles:flyable:{snapshot.pk}"
    cached = cache.get(key)
    if cached is None:
        cached = flyable_ships(snapshot)
        cache.set(key, cached, 600)
    return cached


def _attributes(character) -> dict | None:
    from apps.characters.models import CharacterAttributes
    from apps.sde.models import SdeType

    attrs = CharacterAttributes.objects.filter(character=character).first()
    if attrs is None:
        return None
    implant_ids = []
    for raw in attrs.implants or []:
        try:
            implant_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    names = dict(SdeType.objects.filter(type_id__in=implant_ids).values_list("type_id", "name"))
    return {
        "values": [
            (_("Intelligence"), attrs.intelligence),
            (_("Memory"), attrs.memory),
            (_("Perception"), attrs.perception),
            (_("Willpower"), attrs.willpower),
            (_("Charisma"), attrs.charisma),
        ],
        "implants": [
            {"id": tid, "name": names.get(tid) or _("Implant %(id)s") % {"id": tid}}
            for tid in implant_ids
        ],
        "as_of": attrs.as_of,
    }


def _combat(character) -> dict | None:
    """The public killboard slice: the all-time card plus the last few kills. Everything here
    is derived from killmails, which are public EVE facts."""
    from core.features import feature_enabled

    if not feature_enabled("killboard"):
        return None
    from apps.killboard.leaderboards import pilot_combat_card
    from apps.killboard.models import Killmail, KillmailParticipant

    card = pilot_combat_card(character.character_id)
    # Newest first; a character sits on a killmail at most once, so no distinct() is needed.
    recent_ids = list(
        KillmailParticipant.objects.filter(
            role=KillmailParticipant.Role.ATTACKER,
            character_id=character.character_id,
            killmail__is_npc=False,
        )
        .order_by("-killmail__killmail_time")
        .values_list("killmail_id", flat=True)[:5]
    )
    recent = list(Killmail.objects.filter(killmail_id__in=recent_ids).order_by("-killmail_time"))
    return {"card": card, "recent": recent}


# --------------------------------------------------------------------------- #
#  Throttles — fixed windows in the cache (the signature/killcard pattern).
# --------------------------------------------------------------------------- #
def throttle_ok(bucket: str, key: str, *, limit: int, window: int = 60) -> bool:
    """True while ``key`` is under ``limit`` hits per ``window`` seconds in ``bucket``. The TTL
    is set once by ``add`` and not refreshed by ``incr``, so the window really closes."""
    if limit <= 0:
        return True
    cache_key = f"profiles:throttle:{bucket}:{key or 'anon'}"
    cache.add(cache_key, 0, window)
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.add(cache_key, 1, window)
        count = 1
    return count <= limit
