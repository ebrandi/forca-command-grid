"""Ansiblex jump-bridge registry — the corp/alliance jump-beacon network.

Ansiblex Jump Gates are player-deployed structures that link two systems, usable
like a stargate. They aren't in the SDE and there's no public ESI for another
entity's bridges, so officers register the network here; the route planner then
feeds them to ESI ``/route`` as extra ``connections`` so routes can use them.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class Source(models.TextChoices):
    MANUAL = "manual", _("Manual")
    ESI = "esi", _("ESI sync")


class AnsiblexBridge(TimeStampedModel):
    name = models.CharField(max_length=120, blank=True)
    from_system_id = models.IntegerField()
    from_system_name = models.CharField(max_length=100, blank=True)
    to_system_id = models.IntegerField()
    to_system_name = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)
    structure_id = models.BigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        unique_together = ("from_system_id", "to_system_id")
        ordering = ["from_system_name", "to_system_name"]

    def __str__(self) -> str:
        return f"{self.from_system_name} ⇄ {self.to_system_name}"


class CynoBeacon(TimeStampedModel):
    """A permanent cyno structure (Pharolux Cyno Beacon) — a guaranteed cyno target."""
    system_id = models.IntegerField(unique=True)
    system_name = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=120, blank=True)
    structure_id = models.BigIntegerField(null=True, blank=True, unique=True)
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)

    class Meta:
        ordering = ["system_name"]

    def __str__(self) -> str:
        return f"Cyno beacon · {self.system_name}"


class JumpPlannerConfig(TimeStampedModel):
    """Leadership-tunable defaults for the Jump Planner (singleton).

    Follows the ``.active()`` convention used by IndustryEconomyConfig / SrpProgram:
    one active row, created on first read. The planner reads these for its default
    skill assumptions, route preference, high-sec-exit strategy and safety margin.
    """

    class Preference(models.TextChoices):
        SAFER = "safer", _("Safer (prefer high-sec)")
        SHORTEST = "shortest", _("Shortest")
        INSECURE = "insecure", _("Less secure (prefer low/null)")

    is_active = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    # Default skill assumptions (0–5).
    default_jdc = models.PositiveSmallIntegerField(default=5)
    default_jfc = models.PositiveSmallIntegerField(default=5)
    default_jf_skill = models.PositiveSmallIntegerField(default=5)
    # High-sec exit selection strategy.
    prefer_stations = models.BooleanField(default=True)
    default_preference = models.CharField(max_length=12, choices=Preference.choices, default=Preference.SAFER)
    fuel_safety_margin_pct = models.FloatField(default=0.0)
    # Corp avoid-lists (comma-separated system / region names), merged into user avoids.
    avoid_systems = models.TextField(blank=True)
    avoid_regions = models.TextField(blank=True)
    # Governance / UX.
    allow_pilot_exit_override = models.BooleanField(default=True)
    allow_saved_routes = models.BooleanField(default=True)
    highsec_exit_warning = models.TextField(
        blank=True,
        default="Low-sec exit systems carry real risk — scout the exit and the gate route, "
                "and don't autopilot a loaded jump freighter through low-sec.",
    )

    class Meta:
        verbose_name = _("Jump planner config")

    def __str__(self) -> str:
        return "Jump planner config"

    @classmethod
    def active(cls) -> JumpPlannerConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        return cfg or cls.objects.create(is_active=True)


class SavedJumpRoute(TimeStampedModel):
    """A pilot's saved jump-planner query, optionally shared with leadership.

    Stores the *inputs* (systems, ship, skills, preferences), not a frozen result,
    so re-opening it re-plans against current data (fuel prices, avoid-lists).
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", _("Private (only me)")
        LEADERSHIP = "leadership", _("Shared with leadership")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_jump_routes"
    )
    name = models.CharField(max_length=120)
    origin_system_id = models.IntegerField()
    origin_name = models.CharField(max_length=100)
    dest_system_id = models.IntegerField()
    dest_name = models.CharField(max_length=100)
    ship_key = models.CharField(max_length=24)
    jdc = models.PositiveSmallIntegerField(default=5)
    jfc = models.PositiveSmallIntegerField(default=5)
    jf_skill = models.PositiveSmallIntegerField(default=5)
    jde_rigs = models.PositiveSmallIntegerField(default=0)
    preference = models.CharField(max_length=12, default="safer")
    custom_range = models.FloatField(null=True, blank=True)
    waypoints = models.CharField(max_length=300, blank=True)
    avoid_systems = models.CharField(max_length=300, blank=True)
    avoid_regions = models.CharField(max_length=300, blank=True)
    require_stations = models.BooleanField(default=False)
    exit_system_id = models.IntegerField(null=True, blank=True)
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.PRIVATE)
    note = models.CharField(max_length=200, blank=True)
    # 4.5: opt-in tripwire — DM the owner when a camp/incursion appears on this route's
    # systems. Off by default; ``alerted_sig`` dedups so only a *change* in the threat set
    # re-alerts (advanced only when alerted or cleared).
    watch_enabled = models.BooleanField(default=False)
    alerted_sig = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "-updated_at"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.origin_name} → {self.dest_name})"
