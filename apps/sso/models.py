"""EVE SSO Integration: linked characters, encrypted tokens, scope grants."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.corporation.models import EveCorporation
from core.esi import tokens as token_crypto
from core.mixins import ProvenanceMixin, TimeStampedModel


class EveCharacter(ProvenanceMixin):
    character_id = models.BigIntegerField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="characters",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=200, blank=True)
    # EVE SSO owner hash; changes when the character is transferred to another
    # EVE account. Used to detect (and refuse) a post-transfer login that would
    # otherwise inherit the previous owner's account, roles and data.
    owner_hash = models.CharField(max_length=64, blank=True)
    corporation = models.ForeignKey(
        EveCorporation, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    alliance_id = models.BigIntegerField(null=True, blank=True)
    is_main = models.BooleanField(default=False)
    is_corp_member = models.BooleanField(default=False)
    # In-game corporation Director role, per PILOT (LP-4). This has always been *checked*
    # (services.character_is_corp_director) but only ever stored as an account-level
    # RoleAssignment, so the app could not answer "is THIS pilot a director" — precisely the
    # question pilot switching forces. Without it, a director alt would hand Director authority
    # to every other pilot on the account. Set by the same reconcile that grants the role.
    is_corp_director = models.BooleanField(default=False, db_default=False)
    security_status = models.FloatField(null=True, blank=True)
    affiliation_updated_at = models.DateTimeField(null=True, blank=True)
    # Last time the in-game Director-role ESI check ran for this character (4.8). Drives
    # the staleness-ordered, per-run-capped director reconcile so its ESI cost stays
    # bounded and decoupled from total alt count.
    director_checked_at = models.DateTimeField(null=True, blank=True)
    added_at = models.DateTimeField(default=timezone.now)

    # --- linked-pilot metadata (LP-1) ---
    # ``added_at`` is the link date and ``is_main`` the primary flag, so the only genuinely
    # missing pieces are recency and a user-chosen order. There is no ``link_status`` column:
    # the link's health is a function of live AuthToken state, and a stored copy would be a
    # second source of truth that drifts from the tokens it claims to describe.
    last_used_at = models.DateTimeField(null=True, blank=True)
    display_order = models.SmallIntegerField(default=0, db_default=0)

    class Meta:
        # Back the affiliation + director staleness filters (…IS NULL OR < cutoff,
        # ORDER BY … NULLS FIRST) so the capped sweeps are index scans, not seq-scan+sort.
        indexes = [
            models.Index(fields=["affiliation_updated_at"], name="evechar_affil_idx"),
            models.Index(fields=["director_checked_at"], name="evechar_director_idx"),
        ]

    def __str__(self) -> str:
        return self.name or str(self.character_id)


class AuthToken(TimeStampedModel):
    """OAuth tokens for a character's granted scopes. Refresh token is stored
    ENCRYPTED at rest; the access token is a short-lived cache (also encrypted).
    Multiple tokens per character are allowed (multi-director resilience)."""

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="tokens"
    )
    _refresh_token = models.TextField(db_column="refresh_token", blank=True)
    _access_token = models.TextField(db_column="access_token", blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    token_type = models.CharField(max_length=20, default="Bearer")
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_refresh_ok_at = models.DateTimeField(null=True, blank=True)
    refresh_fail_count = models.PositiveIntegerField(default=0)
    # Last time we re-read this token's CCP-authoritative ``scp`` claim and reconciled
    # our recorded scopes/grants against it (4.7). Null = never verified since mint.
    scopes_verified_at = models.DateTimeField(null=True, blank=True)

    # --- encrypted accessors (never expose ciphertext columns directly) ---
    @property
    def refresh_token(self) -> str:
        return token_crypto.decrypt(self._refresh_token)

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        self._refresh_token = token_crypto.encrypt(value or "")

    @property
    def access_token(self) -> str:
        return token_crypto.decrypt(self._access_token)

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._access_token = token_crypto.encrypt(value or "")

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None and bool(self._refresh_token)

    @property
    def access_expired(self) -> bool:
        if not self.access_expires_at:
            return True
        # 30s safety margin.
        return (self.access_expires_at - timezone.now()).total_seconds() < 30

    def __str__(self) -> str:
        return f"token<{self.character_id}>"


class EveScopeGrant(models.Model):
    """Which ESI scope a character granted and which feature it unlocks."""

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="scope_grants"
    )
    scope = models.CharField(max_length=128)
    feature_key = models.CharField(max_length=64, blank=True)
    granted_at = models.DateTimeField(default=timezone.now)
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("character", "scope")

    def __str__(self) -> str:
        return f"{self.character_id}:{self.scope}"
