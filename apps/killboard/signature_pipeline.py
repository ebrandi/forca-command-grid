"""Combat Signatures — the off-request render pipeline (WS-4, plan A10/A11).

Everything between "a pilot's stats changed" and "a fresh PNG sits on disk for nginx to serve"
lives here as plain domain functions; the thin Celery wrappers in :mod:`apps.killboard.tasks`
call them. Three entry points:

* :func:`signature_tick` — the every-10-minute beat body. A cursor-consumer over the KB-29
  ``KillboardStreamEvent`` ring buffer (the same contract the outbound stream, per-pilot
  subscriptions and trophies use): it marks the live signatures of pilots touched by fresh kills
  dirty, runs the membership freeze/unfreeze sweep, then re-renders a bounded batch of the due
  signatures. Coalesced (one render per signature per tick), debounced, and guarded by a global
  ``cache.add`` mutex so overlapping beats never double-run. Inert until leadership arms the
  feature (one cheap config read).
* :func:`render_one` / :func:`force_render` — render a single signature. ``render_one`` respects
  the per-signature debounce and only touches ACTIVE (or, under an explicit admin ``force``,
  FROZEN) rows; ``force_render`` is the manual-regenerate path (clears the debounce, resets the
  failure ledger, renders now). Both write atomically (tmp + ``os.replace``) and keep the last
  known-good artifact on failure, recording a path-stripped error and a failure counter that
  eventually parks a persistently-failing render until its config changes or it is regenerated.
* :func:`cleanup_orphans` — the media janitor: deletes artifacts with no row, or whose signature
  is disabled, while never touching an active/frozen signature's live image.

The ONLY network this feature ever performs is the worker-side portrait/logo mirror pre-step
(:mod:`signature_assets`), isolated in :func:`_prefetch_assets` and guarded per-asset so a dead
CDN degrades an avatar to a monogram — it never fails a render. The pure renderer
(:mod:`signature_render`) reads only local files.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from . import signature_assets as assets
from . import signatures, stream
from .models import (
    CombatSignature,
    CombatSignatureSettings,
    Killmail,
    KillmailParticipant,
    SignatureScanState,
)
from .signature_render import render_signature_png
from .signature_stats import build_signature_payload

log = logging.getLogger("forca.killboard")

# Global tick mutex (killstream.py:104-113 owner-token idiom) — comfortably above one tick's work.
_TICK_LOCK_KEY = "kb:signature:tick:lock"
_TICK_LOCK_TTL = 300
# Per-(signature, config_version) render debounce — a re-edit (config_version bump) opens a fresh
# window automatically; a plain re-mark within the window coalesces to one render.
_DEBOUNCE_TTL = 600

# Strip absolute-path-like runs from an exception before it lands in the admin-visible render_error
# (threat model A10: no filesystem paths in stored errors). "/srv/media/sig/ab.png.tmp" → "<path>".
_SANITISE_PATH_RE = re.compile(r"/[\w./-]+")

_ATTACKER = Killmail.HomeRole.ATTACKER


# --------------------------------------------------------------------------- #
#  Settings accessors (all env-overridable; see config/settings/base.py)
# --------------------------------------------------------------------------- #
def _feature_enabled() -> bool:
    from core.features import feature_enabled

    return feature_enabled("killboard")


def _max_failures() -> int:
    return int(getattr(settings, "SIGNATURE_RENDER_MAX_FAILURES", 5))


def _max_per_tick() -> int:
    return int(getattr(settings, "SIGNATURE_RENDER_MAX_PER_TICK", 30))


def _scan_batch() -> int:
    return int(getattr(settings, "KILLBOARD_STREAM_BATCH", 200))


def _debounce_key(sig: CombatSignature) -> str:
    return f"kb:signature:render:{sig.pk}:{sig.config_version}"


# --------------------------------------------------------------------------- #
#  Single-signature render (the unit of work)
# --------------------------------------------------------------------------- #
def _sanitise_error(exc: Exception) -> str:
    """A short, path-free rendering of ``exc`` safe to store on the row for admins."""
    return _SANITISE_PATH_RE.sub("<path>", str(exc))[:300]


def _safe_fetch(fetch, entity_id) -> None:
    """Warm one mirror asset, swallowing every failure — a bad asset must degrade to a monogram,
    never fail a render (this absorbs both a network error and a mirror write fault)."""
    try:
        fetch(entity_id)
    except Exception:  # noqa: BLE001 — a failed asset degrades to a monogram, never fails a render
        log.warning("signature asset prefetch failed (%s, %s)",
                    getattr(fetch, "__name__", fetch), entity_id, exc_info=True)


def _prefetch_assets(sig: CombatSignature) -> None:
    """Warm the worker-side portrait/logo mirror for the components this signature actually shows.

    This is the ONLY network step in the pipeline. Each fetch is isolated so a dead CDN or a slow
    logo degrades that asset alone; the payload build that follows then reads the now-warm cache
    (or, for a still-absent asset, falls back to a monogram/omitted logo).
    """
    comps = set((sig.config or {}).get("components", []))
    char = sig.character
    if "portrait" in comps:
        _safe_fetch(assets.ensure_portrait, char.character_id)
    if "corp" in comps and char.corporation_id:
        _safe_fetch(assets.ensure_corp_logo, char.corporation_id)
    if "alliance" in comps and char.alliance_id:
        _safe_fetch(assets.ensure_alliance_logo, char.alliance_id)


def _atomic_write_artifact(token: str, data: bytes) -> None:
    """Write the banner PNG to the signature's artifact path atomically (tmp + ``os.replace``).

    The directory is created 0o755 and the file 0o644 so nginx (a different uid on a read-only
    media mount in prod) can serve it. On any failure the temp file is removed and the previous
    artifact (the last known-good image) is left untouched.
    """
    path = signatures.artifact_path(token)  # ValueError on a bad token — never for a real row
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
        try:
            # nginx serves the artifacts as a different uid from a read-only media mount, so the
            # directory must be world-traversable (the files below are written 0o644 for the same
            # reason). Servable-but-not-writable, exactly the eveimg mirror's posture.
            os.chmod(directory, 0o755)  # noqa: S103 - required for nginx (other-uid, ro) to serve
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def render_one(signature_id: int, *, force: bool = False) -> str:
    """Render one signature to disk. Returns ``rendered`` / ``skipped_debounce`` /
    ``skipped_inactive`` / ``failed``.

    Only ACTIVE signatures render on the normal path; a FROZEN signature renders only under an
    explicit admin ``force`` (its public image is otherwise left frozen in place), and a DISABLED
    or missing row never renders. A ``force`` render also clears the debounce so a manual
    regenerate always produces a fresh image. On success the row is reset to clean/OK; on failure
    the last known-good artifact is kept and the failure ledger advances — the tick's picker parks
    a signature that has failed ``SIGNATURE_RENDER_MAX_FAILURES`` times until its config changes or
    it is regenerated. Never raises: a render fault must not escape the beat loop.
    """
    sig = (
        CombatSignature.objects.select_related("character", "background")
        .filter(pk=signature_id)
        .first()
    )
    if sig is None:
        return "skipped_inactive"
    if sig.status == CombatSignature.Status.ACTIVE:
        pass
    elif sig.status == CombatSignature.Status.FROZEN and force:
        pass
    else:
        return "skipped_inactive"

    key = _debounce_key(sig)
    if force:
        cache.delete(key)
    if not cache.add(key, "1", timeout=_DEBOUNCE_TTL):
        return "skipped_debounce"

    try:
        _prefetch_assets(sig)
        payload = build_signature_payload(sig)
        png = render_signature_png(sig, payload)
        _atomic_write_artifact(sig.public_token, png)
    except Exception as exc:  # noqa: BLE001 — never let a render fault escape the beat loop
        sig.render_status = CombatSignature.RenderStatus.FAILED
        sig.consecutive_failures = (sig.consecutive_failures or 0) + 1
        sig.render_error = _sanitise_error(exc)
        sig.save(update_fields=[
            "render_status", "consecutive_failures", "render_error", "updated_at",
        ])
        log.warning("signature render failed for %s (failure #%s)",
                    sig.pk, sig.consecutive_failures, exc_info=True)
        return "failed"

    sig.dirty = False
    sig.render_status = CombatSignature.RenderStatus.OK
    sig.rendered_at = timezone.now()
    sig.consecutive_failures = 0
    sig.render_error = ""
    sig.save(update_fields=[
        "dirty", "render_status", "rendered_at", "consecutive_failures",
        "render_error", "updated_at",
    ])
    return "rendered"


def force_render(signature_id: int) -> str:
    """Manual regenerate (WS-6 editor button / admin console): clear the debounce, reset the
    failure ledger, and render now — even a frozen signature. Returns the :func:`render_one` status.
    """
    sig = CombatSignature.objects.filter(pk=signature_id).first()
    if sig is None:
        return "skipped_inactive"
    cache.delete(_debounce_key(sig))
    if sig.consecutive_failures:
        sig.consecutive_failures = 0
        sig.render_error = ""
        sig.save(update_fields=["consecutive_failures", "render_error", "updated_at"])
    return render_one(signature_id, force=True)


# --------------------------------------------------------------------------- #
#  The refresh tick (beat body)
# --------------------------------------------------------------------------- #
def _touched_attacker_ids(kill_km_ids: list[int]) -> set[int]:
    """The distinct home-corp attacker character ids on ``kill_km_ids`` (one indexed query).

    Stream events carry no attacker ids (only the victim), so — exactly like the trophy scan — we
    join back through ``KillmailParticipant`` to learn which home pilots earned those kills.
    """
    if not kill_km_ids:
        return set()
    return set(
        KillmailParticipant.objects.filter(
            killmail_id__in=kill_km_ids,
            role=KillmailParticipant.Role.ATTACKER,
            corporation_id=settings.FORCA_HOME_CORP_ID,
            character_id__isnull=False,
        ).values_list("character_id", flat=True)
    )


def _consume_stream() -> tuple[int, int]:
    """Advance the scan cursor over fresh events, marking touched pilots' live signatures dirty.

    Returns ``(events_scanned, signatures_marked)``. Tolerates a prune that outran the cursor by
    fast-forwarding to the tip rather than re-scanning history (the trophy-scan contract). Only
    kills mark a signature dirty on this fast path; a pilot's own losses are picked up by the
    interval refresh in :func:`_render_due` (a loss is not a banner-worthy immediate trigger).
    """
    tip = stream.tip_seq()
    state = SignatureScanState.load()
    if state.last_seq >= tip:
        return 0, 0
    batch = list(
        stream.KillboardStreamEvent.objects.filter(seq__gt=state.last_seq)
        .order_by("seq")[: _scan_batch()]
    )
    if not batch:
        # The ring buffer was pruned past the cursor — fast-forward so we don't re-scan history.
        state.last_seq = tip
        state.save(update_fields=["last_seq", "updated_at"])
        return 0, 0
    processed_tip = batch[-1].seq
    kill_km_ids = [ev.killmail_id for ev in batch if ev.home_role == _ATTACKER]
    touched = _touched_attacker_ids(kill_km_ids)
    marked = 0
    if touched:
        marked = CombatSignature.objects.filter(
            character_id__in=touched,
            status=CombatSignature.Status.ACTIVE,
            mode=CombatSignature.Mode.LIVE,
        ).update(dirty=True, updated_at=timezone.now())
    state.last_seq = processed_tip
    state.save(update_fields=["last_seq", "updated_at"])
    return len(batch), marked


def _membership_sweep(cfg: CombatSignatureSettings) -> tuple[int, int]:
    """Reconcile signatures against corp membership (A11). Returns ``(unfrozen, frozen)``.

    A frozen signature whose owner has rejoined is reactivated (and queued for a fresh render); an
    active signature whose owner has left is frozen — or, when ``revoke_on_leave`` is set, disabled
    and its image deleted. The affiliation sweep keeps ``is_corp_member`` ≤6h fresh, so this runs
    every tick as a cheap membership delta.
    """
    unfrozen = 0
    for sig in CombatSignature.objects.filter(
        status=CombatSignature.Status.FROZEN, character__is_corp_member=True
    ).select_related("character"):
        signatures.unfreeze(sig)
        unfrozen += 1
    frozen = 0
    revoke = bool(cfg.revoke_on_leave)
    for sig in CombatSignature.objects.filter(
        status=CombatSignature.Status.ACTIVE, character__is_corp_member=False
    ).select_related("character"):
        signatures.freeze(sig, revoke=revoke)
        frozen += 1
    return unfrozen, frozen


def _render_due(cfg: CombatSignatureSettings) -> tuple[int, int, int]:
    """Render the due signatures, oldest first, up to the per-tick cap. Returns
    ``(rendered, failed, skipped)``.

    A signature is due when it is ACTIVE and either dirty (and not parked by the failure ledger) —
    which covers snapshots too — or a live banner whose last render predates the refresh interval.
    Snapshots never interval-refresh; they render only while dirty.
    """
    max_failures = _max_failures()
    cap = _max_per_tick()
    cutoff = timezone.now() - timedelta(hours=cfg.refresh_interval_hours)
    due_ids = list(
        CombatSignature.objects.filter(status=CombatSignature.Status.ACTIVE)
        .filter(
            Q(dirty=True, consecutive_failures__lt=max_failures)
            | Q(mode=CombatSignature.Mode.LIVE, rendered_at__lt=cutoff)
        )
        .order_by(F("rendered_at").asc(nulls_first=True))
        .values_list("pk", flat=True)[:cap]
    )
    rendered = failed = skipped = 0
    for sid in due_ids:
        status = render_one(sid)
        if status == "rendered":
            rendered += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1
    return rendered, failed, skipped


def _run_tick(cfg: CombatSignatureSettings) -> dict:
    scanned, marked = _consume_stream()
    unfrozen, frozen = _membership_sweep(cfg)
    rendered, failed, skipped = _render_due(cfg)
    return {
        "status": "ok",
        "scanned": scanned,
        "marked_dirty": marked,
        "unfrozen": unfrozen,
        "frozen": frozen,
        "rendered": rendered,
        "failed": failed,
        "skipped": skipped,
    }


def signature_tick() -> dict:
    """The Combat Signatures refresh tick (every 10 min). No-op unless the feature is armed.

    Inert-until-armed: a single cheap config read returns immediately when leadership has not
    enabled the feature (or the killboard feature is off). A global ``cache.add`` mutex (released
    in ``finally``) serialises overlapping beats so the dirty-marking, membership sweep and render
    batch never double-run.
    """
    cfg = CombatSignatureSettings.load()
    if not cfg.enabled or not _feature_enabled():
        return {"status": "disabled"}
    token = uuid4().hex
    if not cache.add(_TICK_LOCK_KEY, token, timeout=_TICK_LOCK_TTL):
        return {"status": "locked"}
    try:
        return _run_tick(cfg)
    finally:
        if cache.get(_TICK_LOCK_KEY) == token:  # only free our own lock (overrun-safe)
            cache.delete(_TICK_LOCK_KEY)


# --------------------------------------------------------------------------- #
#  Media janitor
# --------------------------------------------------------------------------- #
def cleanup_orphans() -> int:
    """Delete orphaned signature artifacts from ``MEDIA_ROOT/signatures``. Returns the count
    removed, or ``-1`` when the directory is missing (refuses to run rather than guess).

    An artifact is an orphan when its token maps to no signature at all, or to a DISABLED one
    (disable/rotate already delete eagerly; this is the janitor for crash-orphaned files). A file
    whose token belongs to an ACTIVE or FROZEN signature — its live image — is never deleted, and a
    file whose name is not a valid signature token is left untouched (never our artifact).
    """
    directory = os.path.join(settings.MEDIA_ROOT, "signatures")
    if not os.path.isdir(directory):
        log.warning("signature orphan cleanup: %s is missing — refusing to run", directory)
        return -1
    keep = {CombatSignature.Status.ACTIVE, CombatSignature.Status.FROZEN}
    status_by_token = dict(CombatSignature.objects.values_list("public_token", "status"))
    removed = 0
    for name in os.listdir(directory):
        if not name.endswith(".png"):
            continue
        token = name[:-4]
        if not signatures._TOKEN_RE.match(token):
            continue  # not a signature artifact — leave foreign files alone
        if status_by_token.get(token) in keep:
            continue  # an active/frozen signature's live image — never delete
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            log.warning("signature orphan cleanup: could not remove %s", name, exc_info=True)
    return removed
