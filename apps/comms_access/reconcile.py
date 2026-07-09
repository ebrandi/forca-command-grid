"""Reconcile engine — diff a pilot's *desired* comms access against their *actual* access
and apply the minimal change, honouring every safety rail.

Invariants (see ``models`` docstring for the why):

* **Managed-set boundary** — ``add``/``remove`` are always intersected with the set of
  refs that appear in an enabled :class:`EntitlementMapping`. Un-mapped roles are untouchable.
* **Additive-default** — a ref is only ever *removed* when one of its mappings is
  ``authoritative``. ``additive`` mappings grant, never revoke.
* **Dry-run-first** — a ref is applied only when neither the global switch nor any mapping
  targeting it is in dry-run; otherwise the intended change is recorded as a ``DRY_RUN`` preview.
* **Pin** — a pinned account is skipped entirely.
* **Inert** — no provider registered / platform not armed ⇒ ``SKIPPED``.

The engine never raises: provider calls are best-effort and failures land as ``FAILED``
ledger rows with a redacted detail.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import IntegrityError
from django.utils import timezone

from core.audit import audit_log

from . import config
from .entitlements import entitlements
from .models import AccessSyncLedger, CommsAccount, EntitlementMapping, MappingMode, SyncAction, SyncResult
from .providers import provider_class


@dataclass
class ReconcileResult:
    skipped: bool = False
    reason: str = ""
    added: set = field(default_factory=set)
    removed: set = field(default_factory=set)
    preview_add: set = field(default_factory=set)
    preview_remove: set = field(default_factory=set)
    failed: set = field(default_factory=set)
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _run_token(source_ref: str = "") -> str:
    """A per-minute run token so ledger writes are idempotent under task redelivery
    (same minute ⇒ deduped by the unique constraint) without deduping across real runs."""
    if source_ref:
        return source_ref[:80]
    return f"run:{timezone.now():%Y%m%d%H%M}"


def _resolve_provider(platform: str, provider=None):
    """Return a provider instance for the platform, or ``None`` to SKIP.

    An explicit ``provider`` (tests / a forced admin action) is always honoured; otherwise
    the platform must be armed in config AND have a registered provider class.
    """
    if provider is not None:
        return provider
    if not config.platform_armed(platform):
        return None
    cls = provider_class(platform)
    if cls is None:
        return None
    pcfg = config.get("platforms").get(platform, {})
    return cls(pcfg)


def _record(account, *, action, result, ref, detail, source_ref) -> None:
    """Idempotent append to the ledger (no-op if this exact event was already recorded)."""
    try:
        AccessSyncLedger.objects.get_or_create(
            account=account,
            platform=account.platform,
            target_ref=ref,
            action=action,
            source_ref=source_ref,
            defaults={"result": result, "detail": (detail or "")[:300]},
        )
    except IntegrityError:
        pass


def reconcile_account(
    account: CommsAccount,
    *,
    provider=None,
    force_dry_run: bool = False,
    source_ref: str = "",
    mappings=None,
) -> ReconcileResult:
    """Reconcile a single linked account. Never raises.

    ``mappings`` may be a pre-fetched list of this platform's enabled
    :class:`EntitlementMapping` rows (the periodic sweep resolves them once and passes
    them in to avoid an N+1 query per account); ``None`` means fetch them here.
    """
    if account.pinned:
        return ReconcileResult(skipped=True, reason="account pinned (break-glass)")
    if not account.verified:
        return ReconcileResult(skipped=True, reason="account not verified")

    platform = account.platform
    prov = _resolve_provider(platform, provider)
    if prov is None:
        return ReconcileResult(skipped=True, reason="platform not armed / no provider")

    if mappings is None:
        mappings = list(EntitlementMapping.objects.filter(platform=platform, enabled=True))
    if not mappings:
        return ReconcileResult(skipped=True, reason="no mappings configured")

    ents = entitlements(account.user)
    managed_refs: set[str] = {m.target_ref for m in mappings}
    desired_refs: set[str] = {m.target_ref for m in mappings if m.entitlement_key in ents}
    authoritative_refs: set[str] = {
        m.target_ref for m in mappings if m.mode == MappingMode.AUTHORITATIVE
    }
    # A ref is live (appliable) only when global dry-run is off AND no mapping targeting it
    # is itself in dry-run. To go live, every mapping for a ref must be confirmed.
    global_dry = config.global_dry_run() or force_dry_run
    dry_ref: dict[str, bool] = {}
    for m in mappings:
        dry_ref[m.target_ref] = dry_ref.get(m.target_ref, False) or m.dry_run

    def is_live(ref: str) -> bool:
        return not global_dry and not dry_ref.get(ref, False)

    try:
        current = set(prov.read_current(account)) & managed_refs
    except Exception:  # noqa: BLE001 - best-effort read; treat as unknown
        current = set()

    add_candidates = desired_refs - current
    remove_candidates = (current & authoritative_refs) - desired_refs

    apply_add = {r for r in add_candidates if is_live(r)}
    apply_remove = {r for r in remove_candidates if is_live(r)}
    preview_add = add_candidates - apply_add
    preview_remove = remove_candidates - apply_remove

    token = _run_token(source_ref)
    result = ReconcileResult(preview_add=preview_add, preview_remove=preview_remove)

    # Record previews (idempotent) so the console can show "what would happen".
    for ref in preview_add:
        _record(account, action=SyncAction.GRANT, result=SyncResult.DRY_RUN, ref=ref,
                detail="preview", source_ref=token)
    for ref in preview_remove:
        _record(account, action=SyncAction.REVOKE, result=SyncResult.DRY_RUN, ref=ref,
                detail="preview", source_ref=token)

    if apply_add or apply_remove:
        outcome = prov.apply(account, add=apply_add, remove=apply_remove)
        result.added = set(outcome.applied_add)
        result.removed = set(outcome.applied_remove)
        result.error = outcome.error or ""
        for ref in result.added:
            _record(account, action=SyncAction.GRANT, result=SyncResult.APPLIED, ref=ref,
                    detail="", source_ref=token)
        for ref in result.removed:
            _record(account, action=SyncAction.REVOKE, result=SyncResult.APPLIED, ref=ref,
                    detail="", source_ref=token)
        # Anything we intended to apply but the provider didn't confirm = FAILED.
        for ref in apply_add - result.added:
            result.failed.add(ref)
            _record(account, action=SyncAction.GRANT, result=SyncResult.FAILED, ref=ref,
                    detail=outcome.error, source_ref=token)
        for ref in apply_remove - result.removed:
            result.failed.add(ref)
            _record(account, action=SyncAction.REVOKE, result=SyncResult.FAILED, ref=ref,
                    detail=outcome.error, source_ref=token)

    account.last_synced_at = timezone.now()
    account.last_error = (result.error or "")[:300]
    account.save(update_fields=["last_synced_at", "last_error", "updated_at"])

    if result.changed or result.failed:
        audit_log(
            None, "comms_access.sync.apply",
            target_type="comms_account", target_id=account.pk,
            metadata={
                "platform": platform,
                "added": sorted(result.added),
                "removed": sorted(result.removed),
                "failed": sorted(result.failed),
                "preview_add": sorted(preview_add),
                "preview_remove": sorted(preview_remove),
            },
        )
    return result


def reconcile_user(user, *, force_dry_run: bool = False, source_ref: str = "") -> dict:
    """Reconcile every armed, verified, non-pinned account for one user."""
    results: dict[str, ReconcileResult] = {}
    accounts = CommsAccount.objects.filter(user=user, verified=True, pinned=False)
    for account in accounts:
        results[account.platform] = reconcile_account(
            account, force_dry_run=force_dry_run, source_ref=source_ref
        )
    return results


def iter_reconcilable_accounts():
    """Verified, non-pinned accounts on currently-armed platforms (for the beat sweep)."""
    armed = [p for p in config.PLATFORMS if config.platform_armed(p)]
    if not armed:
        return CommsAccount.objects.none()
    return CommsAccount.objects.filter(
        verified=True, pinned=False, platform__in=armed
    ).select_related("user")
