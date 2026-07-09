"""Access-provider abstraction — the interface every platform adapter implements.

Adapters are **best-effort**: no method raises into the reconcile engine; a failure comes
back as an :class:`ApplyResult` with ``ok=False`` and a *redacted* error (no token / PII).
Outbound network calls run worker-only, each behind its own host allowlist + no-redirect
discipline (reuse ``apps.pingboard.providers._http``).

Read/write is scoped to the **managed set** by the caller: the engine only ever passes
``add``/``remove`` refs that appear in an :class:`~apps.comms_access.models.EntitlementMapping`,
so an adapter never needs to reason about un-managed roles.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class ApplyResult:
    ok: bool = False
    applied_add: set = field(default_factory=set)
    applied_remove: set = field(default_factory=set)
    error: str = ""       # REDACTED, safe to store/log
    skipped: bool = False  # provider not configured / nothing to do (not a failure)


class AccessProvider(abc.ABC):
    platform: str = ""
    supports_link: bool = False   # OAuth account linking available?
    supports_kick: bool = False

    def __init__(self, config: dict | None = None):
        # Non-secret per-platform config (guild id, workspace id, kick flag). Secrets
        # (bot token) are read by the concrete adapter from settings, not from here.
        self.config = config or {}

    @abc.abstractmethod
    def validate_configuration(self) -> tuple[bool, str]:
        """(ok, redacted message) — is this provider ready to act?"""

    @abc.abstractmethod
    def read_current(self, account) -> set[str]:
        """The platform-role refs the pilot currently holds (best-effort; ``set()`` on error)."""

    @abc.abstractmethod
    def apply(self, account, *, add: set[str], remove: set[str]) -> ApplyResult:
        """Grant ``add`` and revoke ``remove`` (both already scoped to the managed set)."""

    def kick(self, account, reason: str = "") -> ApplyResult:
        """Remove the person from the platform entirely. Guarded + off by default."""
        return ApplyResult(ok=False, skipped=True, error="kick not supported")
