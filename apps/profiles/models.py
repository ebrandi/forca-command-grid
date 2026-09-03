"""Public pilot profiles — an opt-in, unlisted page a pilot shares outside the corp.

A pilot who wants to show their skills to someone who is *not* a member — a recruiter in
another corporation, an FC in a public fleet, a friend — publishes a profile for ONE of their
characters. The page lives at an unguessable, token-addressed URL (``/p/<token>/``), is never
listed anywhere, is marked ``noindex`` for crawlers, optionally expires on a date the pilot
picks, and can additionally sit behind a simple password. The pilot chooses which sections the
page carries (``sections`` — see :mod:`apps.profiles.sections`); everything else about the
character stays inside the corp.

What the row is NOT: it is not a per-account setting and it never mentions the account's other
pilots. A visitor learns about exactly one character and nothing about who else that human flies
(LP-3 — nothing about a pilot is ever merged with another). The ``owner`` FK is the account that
published it, and the public view re-checks on every hit that the character is *still* linked to
that account, so an unlinked or re-linked character can never be served under the old link.
"""
from __future__ import annotations

import re
import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone

from apps.sso.models import EveCharacter
from core.mixins import TimeStampedModel

# ``secrets.token_urlsafe(24)`` → 32 URL-safe characters (192 bits of entropy). The format is
# validated BEFORE any database access on the public route, so a malformed token 404s for free.
TOKEN_BYTES = 24
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")

NOTE_MAX_LENGTH = 500
PASSWORD_MIN_LENGTH = 4
PASSWORD_MAX_LENGTH = 64


def new_token() -> str:
    """An unguessable, URL-safe public token for a profile."""
    return secrets.token_urlsafe(TOKEN_BYTES)


class PublicProfile(TimeStampedModel):
    """One character's opt-in public profile (see the module docstring)."""

    character = models.OneToOneField(
        EveCharacter, on_delete=models.CASCADE, related_name="public_profile"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="public_profiles"
    )
    token = models.CharField(max_length=48, unique=True, default=new_token)
    enabled = models.BooleanField(default=True)
    # NULL = never expires. Compared against ``timezone.now()`` on every public hit.
    expires_at = models.DateTimeField(null=True, blank=True)
    # Django password-hasher output (PBKDF2 by default); blank = no password. Only ever the
    # hash — the plaintext is never stored and never re-displayed to the owner.
    password_hash = models.CharField(max_length=128, blank=True)
    # The section keys the page carries, in registry order (apps.profiles.sections).
    sections = models.JSONField(default=list, blank=True)
    # An optional plain-text note from the pilot to the visitor ("Applying for your incursion
    # community — EU timezone"). Rendered escaped, never as markup.
    note = models.TextField(blank=True, max_length=NOTE_MAX_LENGTH)
    # Visitor bookkeeping the owner sees on the management page. Owner previews never count.
    view_count = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"public profile of {self.character_id} ({'on' if self.enabled else 'off'})"

    # --- state -------------------------------------------------------------------------- #
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_live(self) -> bool:
        """Enabled and not past its expiry. The public resolver checks MORE than this (the
        character must still be linked to the owner, who must still be a member)."""
        return self.enabled and not self.is_expired

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    # --- secrets ------------------------------------------------------------------------ #
    def set_password(self, raw: str | None) -> None:
        """Hash and store ``raw``; ``None``/empty clears the password."""
        self.password_hash = make_password(raw) if raw else ""

    def check_password(self, raw: str) -> bool:
        if not self.password_hash:
            return False
        return check_password(raw or "", self.password_hash)

    def password_fingerprint(self) -> str:
        """A non-secret identity for the *current* password, stored in a visitor's session grant.

        The grant carries this, not a boolean, so changing or clearing the password invalidates
        every grant issued against the old one without touching visitors' sessions."""
        return self.password_hash[-24:] if self.password_hash else ""

    def rotate_token(self) -> str:
        """Mint a fresh token; the old URL dies the moment this is saved."""
        self.token = new_token()
        return self.token
