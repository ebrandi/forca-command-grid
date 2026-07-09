"""External comms access sync — data model.

FORCA holds the authoritative answer to "who is a current member and what are they
entitled to" (``apps.corporation.access`` + ``core.rbac`` + ``apps.identity``). This
subsystem reconciles that *desired* access against the *actual* roles/groups a pilot
holds on an external comms platform (Discord first; Slack/Mumble evaluated).

Safety rails baked into the schema:

* **Managed-set boundary** — the sync only ever touches a role/group that appears as an
  :class:`EntitlementMapping`. Anything not mapped is invisible to the reconcile and can
  never be added or removed. This protects manually-assigned roles, other bots' roles and
  admin roles.
* **Additive by default** — an :class:`EntitlementMapping` only *removes* a role when its
  ``mode`` is ``authoritative`` (opt-in per mapping). ``additive`` mappings grant, never revoke.
* **Ships inert** — nothing acts until a platform is armed in config *and* a provider is
  registered; the reconcile engine SKIPs otherwise.
* **Per-pilot break-glass** — a pinned :class:`CommsAccount` is exempt from all automation.
* **Idempotent, append-only ledger** — :class:`AccessSyncLedger` is the audit of applied
  changes, deduped by a unique constraint like ``raffle.RaffleTicketLedgerEntry``.

Per-account OAuth tokens (Discord/Slack) are Fernet-encrypted at rest via the same
``core.esi.tokens`` helper the SSO app uses; the secret is never surfaced in UI/logs/audit.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.mixins import TimeStampedModel

log = logging.getLogger("forca.comms_access")


class Platform(models.TextChoices):
    DISCORD = "discord", "Discord"
    SLACK = "slack", "Slack"
    MUMBLE = "mumble", "Mumble"


class MappingMode(models.TextChoices):
    ADDITIVE = "additive", "Additive (grant only)"
    AUTHORITATIVE = "authoritative", "Authoritative (grant + remove)"


class SyncAction(models.TextChoices):
    GRANT = "grant", "Grant"
    REVOKE = "revoke", "Revoke"
    KICK = "kick", "Kick"


class SyncResult(models.TextChoices):
    APPLIED = "applied", "Applied"
    DRY_RUN = "dry_run", "Dry run (preview)"
    SKIPPED = "skipped", "Skipped"
    FAILED = "failed", "Failed"


class CommsAccount(TimeStampedModel):
    """One pilot's link to one external comms platform.

    ``external_id`` is the platform-side user id (Discord snowflake / Slack user id /
    Mumble registered-user id). ``_secret`` optionally holds that account's OAuth tokens
    (JSON) encrypted at rest. A ``pinned`` account is exempt from all automated sync
    (break-glass for service accounts / leadership alts).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comms_accounts"
    )
    platform = models.CharField(max_length=16, choices=Platform.choices, db_index=True)
    external_id = models.CharField(max_length=64, blank=True, default="")
    external_handle = models.CharField(max_length=120, blank=True, default="")

    verified = models.BooleanField(default=False)  # link proven via OAuth
    pinned = models.BooleanField(default=False)     # break-glass: exempt from sync

    linked_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")  # REDACTED

    # Encrypted per-account OAuth tokens (JSON) — never surfaced in the UI/API/logs/audit.
    _secret = models.TextField(db_column="secret_enc", blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "platform"], name="uniq_comms_account_user_platform"),
        ]
        indexes = [
            models.Index(fields=["platform", "verified"]),
            models.Index(fields=["platform", "external_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_platform_display()}: {self.external_handle or self.external_id or self.user_id}"

    # -- secret accessors (Fernet at rest; degrade to "" on any failure) --------
    @property
    def secret(self) -> str:
        if not self._secret:
            return ""
        from core.esi.tokens import decrypt

        try:
            return decrypt(self._secret)
        except Exception:  # noqa: BLE001 - a bad/rotated key must not crash a reconcile
            log.warning("CommsAccount %s: secret decrypt failed", self.pk)
            return ""

    @secret.setter
    def secret(self, value: str) -> None:
        from core.esi.tokens import encrypt

        self._secret = encrypt(value or "")

    @property
    def has_secret(self) -> bool:
        return bool(self._secret)

    def get_tokens(self) -> dict:
        """Decrypt and parse the stored OAuth-token blob (``{}`` if none/invalid)."""
        raw = self.secret
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def set_tokens(self, tokens: dict) -> None:
        self.secret = json.dumps(tokens or {})


class PlatformCredential(TimeStampedModel):
    """Console-managed credentials for one comms platform.

    So leadership can stand the integration up **without shell/.env access**. The two secret
    values (bot token, OAuth client secret) are Fernet-encrypted at rest via
    ``core.esi.tokens`` and are **write-only** in the UI — the console shows whether each is
    configured, never the value. The non-secret OAuth fields (client id, callback URL) are
    stored in the clear so they can be displayed and edited.

    These console rows take precedence over the historical env settings
    (``DISCORD_BOT_TOKEN`` / ``DISCORD_OAUTH_*``), which remain an optional fallback so
    env-based deployments keep working. Resolution lives in :mod:`apps.comms_access.credentials`.
    """

    platform = models.CharField(max_length=16, choices=Platform.choices, unique=True)
    oauth_client_id = models.CharField(max_length=128, blank=True, default="")
    oauth_callback_url = models.CharField(max_length=300, blank=True, default="")

    # Fernet-encrypted, write-only — never surfaced in UI/API/logs/audit.
    _bot_token = models.TextField(db_column="bot_token_enc", blank=True, default="")
    _oauth_client_secret = models.TextField(db_column="oauth_client_secret_enc", blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["platform"], name="uniq_comms_credential_platform"),
        ]

    def __str__(self) -> str:
        return f"{self.get_platform_display()} credentials"

    # -- secret accessors (Fernet at rest; degrade to "" on failure) ------------
    @staticmethod
    def _decrypt(blob: str) -> str:
        if not blob:
            return ""
        from core.esi.tokens import decrypt

        try:
            return decrypt(blob)
        except Exception:  # noqa: BLE001 - a bad/rotated key must not crash a reconcile
            log.warning("PlatformCredential: secret decrypt failed")
            return ""

    @staticmethod
    def _encrypt(value: str) -> str:
        # Store an empty string literally so a cleared secret reports as "not configured".
        if not value:
            return ""
        from core.esi.tokens import encrypt

        return encrypt(value)

    @property
    def bot_token(self) -> str:
        return self._decrypt(self._bot_token)

    @bot_token.setter
    def bot_token(self, value: str) -> None:
        self._bot_token = self._encrypt(value or "")

    @property
    def oauth_client_secret(self) -> str:
        return self._decrypt(self._oauth_client_secret)

    @oauth_client_secret.setter
    def oauth_client_secret(self, value: str) -> None:
        self._oauth_client_secret = self._encrypt(value or "")

    @property
    def has_bot_token(self) -> bool:
        return bool(self._bot_token)

    @property
    def has_oauth_client_secret(self) -> bool:
        return bool(self._oauth_client_secret)


class EntitlementMapping(TimeStampedModel):
    """The managed-set boundary: one (entitlement → platform target) rule.

    ``entitlement_key`` is drawn from :mod:`apps.comms_access.entitlements` (``member``,
    ``officer``, ``director``, ``recruiter``, ``fc``, ``alliance``). ``target_ref`` is the
    platform's own id/name for the role/group (a Discord role id, a Slack usergroup id, a
    Mumble group name). Only refs that appear here are ever touched by the reconcile.
    """

    platform = models.CharField(max_length=16, choices=Platform.choices, db_index=True)
    entitlement_key = models.CharField(max_length=48)
    target_type = models.CharField(max_length=24, default="role")  # role|usergroup|group
    target_ref = models.CharField(max_length=64)
    target_label = models.CharField(max_length=120, blank=True, default="")

    mode = models.CharField(max_length=16, choices=MappingMode.choices, default=MappingMode.ADDITIVE)
    dry_run = models.BooleanField(default=True)   # new mappings preview only until confirmed
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["platform", "entitlement_key", "target_ref"]
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "entitlement_key", "target_ref"],
                name="uniq_comms_mapping",
            ),
        ]
        indexes = [models.Index(fields=["platform", "enabled"])]

    def __str__(self) -> str:
        return f"{self.platform}:{self.entitlement_key}→{self.target_label or self.target_ref} ({self.mode})"


class AccessSyncLedger(TimeStampedModel):
    """Append-only, idempotent record of every access change the reconcile applied.

    Corrections are new rows, never edits. The unique constraint makes re-recording the
    same logical event (Celery ACKS_LATE redelivery, a repeated reconcile within one run)
    a no-op — the exact ``raffle.RaffleTicketLedgerEntry`` / ``pingboard.AlertDelivery`` idiom.
    """

    account = models.ForeignKey(CommsAccount, on_delete=models.CASCADE, related_name="ledger")
    platform = models.CharField(max_length=16, choices=Platform.choices, db_index=True)
    target_ref = models.CharField(max_length=64)
    action = models.CharField(max_length=8, choices=SyncAction.choices)
    result = models.CharField(max_length=8, choices=SyncResult.choices)
    detail = models.CharField(max_length=300, blank=True, default="")  # REDACTED
    source_ref = models.CharField(max_length=80, blank=True, default="")  # stable event/run id
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "platform", "target_ref", "action", "source_ref"],
                name="uniq_comms_sync_event",
            ),
        ]
        indexes = [models.Index(fields=["platform", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.platform} {self.action} {self.target_ref} → {self.result}"
