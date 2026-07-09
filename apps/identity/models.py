"""Identity & Access: users, roles, permissions, role assignments."""
from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from core import rbac
from core.mixins import TimeStampedModel


class User(AbstractUser):
    """Platform account. EVE identity comes from linked characters (no EVE
    password is ever stored)."""

    # EVE character id of the chosen "main" (not a Django FK to avoid an
    # import cycle with the sso app).
    main_character_id = models.BigIntegerField(null=True, blank=True)

    def max_role_rank(self) -> int:
        """Highest role rank this user holds (used by core.rbac).

        Memoised on the instance: ``core.rbac.effective_rank`` / ``has_role`` are called
        ~5-6× per authenticated request (the ``roles`` context processor plus the
        Membership/Feature gate middlewares), and each call otherwise re-queries this
        user's role assignments. The memo is request-scoped in effect — a fresh ``User``
        instance is loaded each request — and is invalidated on the loaded instance when a
        ``RoleAssignment`` for it is written (see ``_invalidate_role_rank_cache``)."""
        cached = self.__dict__.get("_max_role_rank_cache")
        if cached is not None:
            return cached
        if self.is_superuser:
            rank = rbac.ROLE_RANK[rbac.ROLE_ADMIN]
        else:
            ranks = [rbac.ROLE_RANK[rbac.ROLE_PUBLIC]]
            now = timezone.now()
            # Use the caller's prefetched assignments when present (e.g. the members console
            # ranks every listed user — a per-user query here would be an N+1); otherwise a
            # single select_related query keeps the per-request hot path at one query. The
            # prefetch must include ``role`` (``role_assignments__role``) so reading
            # ``assignment.role`` below stays query-free.
            prefetched = getattr(self, "_prefetched_objects_cache", {}).get("role_assignments")
            assignments = (
                prefetched if prefetched is not None
                else self.role_assignments.select_related("role").all()
            )
            for assignment in assignments:
                if assignment.expires_at and assignment.expires_at < now:
                    continue
                ranks.append(rbac.ROLE_RANK.get(assignment.role.key, 0))
            rank = max(ranks)
        self.__dict__["_max_role_rank_cache"] = rank
        return rank

    @property
    def role_keys(self) -> list[str]:
        return list(self.role_assignments.values_list("role__key", flat=True))

    def active_permission_keys(self) -> set[str]:
        """Capability keys granted by this user's NON-expired role assignments — the
        least-privilege layer (4.16). Superusers implicitly hold everything, so callers
        short-circuit on is_superuser; this returns only *explicit* grants. Honours
        ``expires_at`` (like max_role_rank) and is memoised + invalidated on the same
        RoleAssignment signal. One assignments query + one prefetch for permissions."""
        cached = self.__dict__.get("_perm_keys_cache")
        if cached is not None:
            return cached
        keys: set[str] = set()
        now = timezone.now()
        for assignment in (
            self.role_assignments.select_related("role").prefetch_related("role__permissions").all()
        ):
            if assignment.expires_at and assignment.expires_at < now:
                continue
            keys.update(p.key for p in assignment.role.permissions.all())
        self.__dict__["_perm_keys_cache"] = keys
        return keys

    @property
    def main_character(self):
        """The chosen main character (else the first linked), or None.

        Used to resolve an account to a pilot name + killboard link in the UI,
        since the username is an opaque ``eve:<id>`` string. Iterates the
        relation (not ``.filter``) so a ``prefetch_related("characters")``
        actually short-circuits the query — display_name is rendered per row
        in leaderboards/feeds, where a per-user query is an N+1.
        """
        chars = list(self.characters.all())
        return next((c for c in chars if c.is_main), chars[0] if chars else None)

    @property
    def display_name(self) -> str:
        """Friendly name: the main character's name, else first name/username."""
        char = self.main_character
        if char:
            return char.name
        return self.first_name or self.username


class Permission(models.Model):
    key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=200, blank=True)

    def __str__(self) -> str:
        return self.key


class Role(models.Model):
    key = models.CharField(max_length=32, unique=True)
    label = models.CharField(max_length=200, blank=True)
    rank = models.IntegerField(default=0)
    permissions = models.ManyToManyField(Permission, blank=True, related_name="roles")

    def __str__(self) -> str:
        return self.key


class RoleAssignment(TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="assignments")
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="grants_made"
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("user", "role")

    def __str__(self) -> str:
        return f"{self.user} → {self.role}"


class RoleChangeRequest(TimeStampedModel):
    """A pending grant of a DUAL-CONTROL role (4.17): a director requests it, a *different*
    director must approve before it applies — so one compromised director can't unilaterally
    mint another. Non-dual-control changes never create a request (they apply immediately).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    target = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_requests")
    role_key = models.CharField(max_length=32)
    # Carried through so approval applies the same time-limited grant the requester intended.
    expires_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_requests_made")
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="role_requests_decided"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        # At most one OPEN request per (target, role) — a partial unique index so re-requesting
        # is blocked while one is pending but a new one is allowed after a decision.
        constraints = [
            models.UniqueConstraint(
                fields=["target", "role_key"], condition=models.Q(status="pending"),
                name="uniq_open_role_request",
            ),
        ]

    def __str__(self) -> str:
        return f"grant {self.role_key} → {self.target} ({self.status})"


from django.db.models.signals import post_delete, post_save  # noqa: E402
from django.dispatch import receiver  # noqa: E402


@receiver(post_save, sender=RoleAssignment)
@receiver(post_delete, sender=RoleAssignment)
def _invalidate_role_rank_cache(sender, instance, **kwargs):
    """Drop the memoised rank on an *already-loaded* user instance when its roles change.

    Reads the cached FK from ``_state.fields_cache`` so it never issues a query: it only
    clears the common ``RoleAssignment.objects.create(user=obj)`` / ``obj.role_assignments``
    path where the same ``User`` instance is held in memory. Other instances are reloaded
    fresh on their next request, so they were never stale."""
    user = instance._state.fields_cache.get("user")
    if user is not None:
        user.__dict__.pop("_max_role_rank_cache", None)
        user.__dict__.pop("_perm_keys_cache", None)  # 4.16: same invalidation for the perm memo
