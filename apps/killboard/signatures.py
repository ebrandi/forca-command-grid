"""Combat Signatures — domain logic for pilot-authored public banner images.

Owns everything upstream of rendering: the strict config schema + validation, name
sanitisation, per-pilot quotas, the unguessable public token, the lifecycle state machine
(create / duplicate / rotate / disable / enable / snapshot / freeze / unfreeze) and the audit
trail for each mutation. Rendering, the Celery pipeline and the public delivery view live in
later workstreams; the ``artifact_path`` / ``delete_artifact`` helpers are the only file-system
seam here so lifecycle mutations can drop a stale image (a missing file makes the URL 404 by
design). Every ownership decision routes through the active pilot (LP-4 ceiling): editing is
allowed only when the acting pilot IS the owner and is a current home-corp member.
"""
from __future__ import annotations

import os
import re
import secrets
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core import pilots as pilots_mod
from core.audit import audit_log

from .models import CombatSignature, CombatSignatureSettings

# A16 — the closed allowlist of component ids a signature config may list.
COMPONENTS = frozenset({
    "portrait", "pilot_name", "corp", "alliance",
    "kills", "losses", "solo_kills", "final_blows",
    "isk_destroyed", "isk_lost", "isk_efficiency", "kd_ratio",
    "rank_title", "rank_progress", "trophies_featured", "trophy_count",
    "last_kill", "best_kill", "favourite_ship", "top_ship_class",
    "activity_period_label", "stats_timestamp",
})

# A15 layout capacity is enforced by the builder (WS-6); the hard cap lives here.
MAX_COMPONENTS = 12

# Activity windows a signature may summarise — mirrors ``leaderboards.WINDOW_KEYS``.
PERIODS = frozenset({"7d", "30d", "90d", "month", "lastmonth", "all"})

# Config colour themes (A16). Distinct from the layout/size-preset model fields.
THEMES = frozenset({"gold", "cyan", "kill"})

# The public artifact filename is derived ONLY from a token in this shape — never from user
# text (threat model: path traversal / unsafe filenames). token_urlsafe(16) is 22 chars.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,24}$")
# Public alias so the WS-5 delivery view validates the token against the same pattern without
# duplicating it. Keep the charset/length in lock-step with the nginx location regex.
TOKEN_RE = _TOKEN_RE

_ALLOWED_CONFIG_KEYS = frozenset({
    "components", "period", "featured_trophy_ids", "show_timestamp", "theme",
})


# --------------------------------------------------------------------------- #
#  Validation
# --------------------------------------------------------------------------- #
def validate_config(config, *, settings, background, layout, size_preset) -> dict:
    """Strictly validate and normalise a signature's config JSON (plan A16).

    ``settings`` is a :class:`CombatSignatureSettings` instance (its ``max_featured_trophies``
    and ``allowed_size_presets`` bound the config). Returns a clean dict with exactly the
    allowed keys, in canonical order. Raises :class:`~django.core.exceptions.ValidationError`
    with a stable, translatable message on the first violation.
    """
    if not isinstance(config, dict):
        raise ValidationError(_("Signature configuration must be an object."))

    unknown = set(config) - _ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValidationError(
            _("Unknown configuration key: %(key)s") % {"key": sorted(unknown)[0]}
        )

    # components — ordered, allowlisted, unique, capped.
    components = config.get("components", [])
    if not isinstance(components, list):
        raise ValidationError(_("Components must be a list."))
    if len(components) > MAX_COMPONENTS:
        raise ValidationError(
            _("A signature may show at most %(n)d components.") % {"n": MAX_COMPONENTS}
        )
    seen: set[str] = set()
    cleaned_components: list[str] = []
    for item in components:
        if not isinstance(item, str):
            raise ValidationError(_("Each component must be a string id."))
        if item not in COMPONENTS:
            raise ValidationError(_("Unknown component: %(id)s") % {"id": item})
        if item in seen:
            raise ValidationError(_("Duplicate component: %(id)s") % {"id": item})
        seen.add(item)
        cleaned_components.append(item)

    # period
    period = config.get("period", "30d")
    if not isinstance(period, str) or period not in PERIODS:
        raise ValidationError(_("Invalid activity period."))

    # featured_trophy_ids — bounded list of distinct integers (bool is not an int here).
    featured = config.get("featured_trophy_ids", [])
    if not isinstance(featured, list):
        raise ValidationError(_("Featured trophies must be a list."))
    if len(featured) > settings.max_featured_trophies:
        raise ValidationError(
            _("At most %(n)d featured trophies are allowed.")
            % {"n": settings.max_featured_trophies}
        )
    seen_tid: set[int] = set()
    cleaned_featured: list[int] = []
    for tid in featured:
        if isinstance(tid, bool) or not isinstance(tid, int):
            raise ValidationError(_("Each featured trophy id must be an integer."))
        if tid in seen_tid:
            raise ValidationError(_("Duplicate featured trophy id."))
        seen_tid.add(tid)
        cleaned_featured.append(tid)

    # show_timestamp
    show_timestamp = config.get("show_timestamp", False)
    if not isinstance(show_timestamp, bool):
        raise ValidationError(_("Show timestamp must be true or false."))

    # theme
    theme = config.get("theme", "gold")
    if not isinstance(theme, str) or theme not in THEMES:
        raise ValidationError(_("Invalid theme."))

    # Cross-field sanity on the model-level choices the config is paired with.
    if layout not in CombatSignature.Layout.values:
        raise ValidationError(_("Invalid layout."))
    if size_preset not in CombatSignature.SizePreset.values:
        raise ValidationError(_("Invalid size preset."))
    allowed_presets = settings.allowed_size_presets or []
    if allowed_presets and size_preset not in allowed_presets:
        raise ValidationError(_("That size preset is not currently allowed."))
    if background is not None and not background.enabled:
        raise ValidationError(_("That background is not available."))

    return {
        "components": cleaned_components,
        "period": period,
        "featured_trophy_ids": cleaned_featured,
        "show_timestamp": show_timestamp,
        "theme": theme,
    }


def sanitize_name(raw) -> str:
    """Return a safe display name: strip Unicode control/format chars (incl. bidi overrides),
    collapse whitespace, and enforce 1..60 characters. Raises on empty/oversized input."""
    if not isinstance(raw, str):
        raise ValidationError(_("A name is required."))
    # Drop control (Cc) and format (Cf — includes bidi overrides like U+202E) characters.
    stripped = "".join(ch for ch in raw if unicodedata.category(ch) not in ("Cc", "Cf"))
    # Collapse every run of whitespace to a single space and trim the ends.
    cleaned = " ".join(stripped.split())
    if not cleaned:
        raise ValidationError(_("A name is required."))
    if len(cleaned) > 60:
        raise ValidationError(_("The name must be at most 60 characters."))
    return cleaned


# --------------------------------------------------------------------------- #
#  Quotas & ownership
# --------------------------------------------------------------------------- #
def active_signature_count(character_id) -> int:
    """How many ACTIVE signatures a pilot currently holds (quota basis)."""
    return CombatSignature.objects.filter(
        character_id=character_id, status=CombatSignature.Status.ACTIVE
    ).count()


def check_quota(character_id, *, settings=None) -> None:
    """Raise :class:`ValidationError` if the pilot is at their active-signature cap."""
    cfg = settings or CombatSignatureSettings.load()
    if active_signature_count(character_id) >= cfg.max_active_per_pilot:
        raise ValidationError(
            _("You already have the maximum of %(n)d active signatures.")
            % {"n": cfg.max_active_per_pilot}
        )


def can_edit(user, signature) -> bool:
    """True when ``user`` may edit ``signature``.

    The active pilot (LP-4 ceiling) must BE the owner and be a current home-corp member. A
    linked pilot who is not currently the acting pilot, another account, or an ex-member all
    fail — matching the threat model's owner-scoped mutation rule.
    """
    pilot = pilots_mod.acting_pilot(user)
    if pilot is None or pilot.character_id != signature.character_id:
        return False
    return bool(pilot.is_corp_member)


def require_edit(user, signature) -> None:
    """Raise :class:`ValidationError` unless ``user`` may edit ``signature``."""
    if not can_edit(user, signature):
        raise ValidationError(_("You cannot edit this signature."))


# --------------------------------------------------------------------------- #
#  Artifact filesystem seam (token-guarded)
# --------------------------------------------------------------------------- #
def artifact_path(token: str) -> str:
    """Absolute path to a signature's rendered PNG under ``MEDIA_ROOT/signatures/``.

    The filename is derived ONLY from a token matching the strict regex — never from user
    text — so a traversal / absolute / bad-charset token raises ``ValueError`` instead of
    resolving a path outside the media tree.
    """
    if not _TOKEN_RE.match(token or ""):
        raise ValueError("invalid signature token")
    return os.path.join(settings.MEDIA_ROOT, "signatures", f"{token}.png")


def delete_artifact(token: str) -> bool:
    """Best-effort removal of a signature's rendered artifact.

    Returns ``True`` when a file was removed. A malformed token or an absent/unremovable file
    is treated as 'nothing to delete' — this never raises into a lifecycle mutation.
    """
    try:
        path = artifact_path(token)
    except ValueError:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  Lifecycle
# --------------------------------------------------------------------------- #
def _unique_token() -> str:
    """A fresh public token guaranteed not to collide with an existing signature."""
    for _attempt in range(10):
        token = secrets.token_urlsafe(16)
        if not CombatSignature.objects.filter(public_token=token).exists():
            return token
    raise RuntimeError("could not allocate a unique signature token")


def _audit(actor, action, signature, *, ip="", metadata=None) -> None:
    """Write an audit row for a signature mutation (canonical keys only, never prose)."""
    data = {"character_id": signature.character_id}
    if metadata:
        data.update(metadata)
    audit_log(
        actor, action,
        target_type="combat_signature", target_id=str(signature.pk),
        metadata=data, ip=ip,
    )


@transaction.atomic
def create_signature(user, *, name, background, layout, size_preset, config,
                     language="", mode=None, ip="") -> CombatSignature:
    """Create a signature owned by ``user``'s acting pilot after full validation + quota check."""
    cfg = CombatSignatureSettings.load()
    pilot = pilots_mod.acting_pilot(user)
    if pilot is None or not pilot.is_corp_member:
        raise ValidationError(_("Only home-corp pilots can create signatures."))
    mode = mode or CombatSignature.Mode.LIVE
    if mode == CombatSignature.Mode.SNAPSHOT and not cfg.snapshots_enabled:
        raise ValidationError(_("Snapshot signatures are not enabled."))
    check_quota(pilot.character_id, settings=cfg)
    clean_name = sanitize_name(name)
    clean_config = validate_config(
        config, settings=cfg, background=background, layout=layout, size_preset=size_preset
    )
    signature = CombatSignature(
        character=pilot, name=clean_name, background=background,
        layout=layout, size_preset=size_preset, language=language or "",
        mode=mode, config=clean_config,
    )
    if mode == CombatSignature.Mode.SNAPSHOT:
        signature.snapshot_taken_at = timezone.now()
    signature.save()
    _audit(user, "signatures.create", signature, ip=ip,
           metadata={"mode": mode, "layout": layout, "size_preset": size_preset})
    return signature


@transaction.atomic
def duplicate_signature(user, signature, *, ip="") -> CombatSignature:
    """Clone ``signature`` for the same owner as a fresh LIVE signature (new token, new render)."""
    require_edit(user, signature)
    cfg = CombatSignatureSettings.load()
    check_quota(signature.character_id, settings=cfg)
    copy = CombatSignature(
        character=signature.character,
        name=sanitize_name(f"{signature.name} (copy)"[:60]),
        background=signature.background,
        layout=signature.layout,
        size_preset=signature.size_preset,
        language=signature.language,
        mode=CombatSignature.Mode.LIVE,
        config=dict(signature.config),
    )
    copy.save()
    _audit(user, "signatures.duplicate", copy, ip=ip, metadata={"source_id": signature.pk})
    return copy


@transaction.atomic
def rotate_token(user, signature, *, ip="") -> str:
    """Rotate the public token; the old URL 404s by design. Returns the new token.

    The stale artifact is dropped best-effort and the signature is marked for re-render at the
    new path (the render itself is enqueued by the WS-4 pipeline).
    """
    require_edit(user, signature)
    old_token = signature.public_token
    signature.public_token = _unique_token()
    signature.dirty = True
    signature.render_status = CombatSignature.RenderStatus.PENDING
    signature.rendered_at = None
    signature.save(update_fields=[
        "public_token", "dirty", "render_status", "rendered_at", "updated_at",
    ])
    delete_artifact(old_token)
    _audit(user, "signatures.rotate_token", signature, ip=ip)
    return signature.public_token


@transaction.atomic
def disable(user, signature, *, ip="") -> CombatSignature:
    """Owner-disable a signature: it stops serving (its artifact is removed) but is kept."""
    require_edit(user, signature)
    if signature.status != CombatSignature.Status.DISABLED:
        signature.status = CombatSignature.Status.DISABLED
        signature.save(update_fields=["status", "updated_at"])
        delete_artifact(signature.public_token)
        _audit(user, "signatures.disable", signature, ip=ip)
    return signature


@transaction.atomic
def enable(user, signature, *, ip="") -> CombatSignature:
    """Re-activate a disabled signature (counts against the active quota) and queue a render."""
    require_edit(user, signature)
    if signature.status != CombatSignature.Status.ACTIVE:
        check_quota(signature.character_id)
        signature.status = CombatSignature.Status.ACTIVE
        signature.dirty = True
        signature.render_status = CombatSignature.RenderStatus.PENDING
        signature.save(update_fields=["status", "dirty", "render_status", "updated_at"])
        _audit(user, "signatures.enable", signature, ip=ip)
    return signature


@transaction.atomic
def take_snapshot(user, signature, *, ip="") -> CombatSignature:
    """Convert a live signature to a frozen snapshot (live→snapshot only).

    Stamps ``snapshot_taken_at`` and leaves ``config_version`` untouched so the frozen config
    is pinned; a re-render captures the stats as-of now. Snapshot→live is not supported
    (create a new signature instead).
    """
    require_edit(user, signature)
    if signature.mode == CombatSignature.Mode.SNAPSHOT:
        raise ValidationError(_("This signature is already a snapshot."))
    cfg = CombatSignatureSettings.load()
    if not cfg.snapshots_enabled:
        raise ValidationError(_("Snapshot signatures are not enabled."))
    signature.mode = CombatSignature.Mode.SNAPSHOT
    signature.snapshot_taken_at = timezone.now()
    signature.dirty = True
    signature.render_status = CombatSignature.RenderStatus.PENDING
    signature.save(update_fields=[
        "mode", "snapshot_taken_at", "dirty", "render_status", "updated_at",
    ])
    _audit(user, "signatures.snapshot", signature, ip=ip)
    return signature


@transaction.atomic
def freeze(signature, *, actor=None, ip="", revoke=False) -> CombatSignature:
    """Freeze a signature whose owner has left the corp (A11 membership lifecycle).

    Editing is already blocked by the LP-4 ceiling; freezing stops refresh and (by default)
    keeps the public image up. With ``revoke`` (``settings.revoke_on_leave``) the artifact is
    deleted and the signature disabled. A system action — no ownership check.
    """
    changed = False
    if revoke:
        if signature.status != CombatSignature.Status.DISABLED:
            signature.status = CombatSignature.Status.DISABLED
            changed = True
        delete_artifact(signature.public_token)
    elif signature.status == CombatSignature.Status.ACTIVE:
        signature.status = CombatSignature.Status.FROZEN
        changed = True
    if changed:
        signature.save(update_fields=["status", "updated_at"])
        _audit(actor, "signatures.freeze", signature, ip=ip, metadata={"revoke": bool(revoke)})
    return signature


@transaction.atomic
def unfreeze(signature, *, actor=None, ip="") -> CombatSignature:
    """Restore a frozen signature to active when its owner rejoins (A11). Queues a render."""
    if signature.status == CombatSignature.Status.FROZEN:
        signature.status = CombatSignature.Status.ACTIVE
        signature.dirty = True
        signature.render_status = CombatSignature.RenderStatus.PENDING
        signature.save(update_fields=["status", "dirty", "render_status", "updated_at"])
        _audit(actor, "signatures.unfreeze", signature, ip=ip)
    return signature
