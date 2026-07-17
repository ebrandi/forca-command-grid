"""Supply Command board config.

The board persists NOTHING row-shaped — it is a cached composition over the phases'
own persisted authorities (availability, demand, MRP, orders, hauls, margin). The only
table is this singleton of leadership thresholds.
"""
from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class BoardConfig(TimeStampedModel):
    """Leadership-tunable Supply Command board thresholds. Singleton via ``active()``.

    The one beat (warm + digest) ships INERT behind ``sweep_enabled``. Per-fit reorder
    thresholds are deliberately absent — those knobs live on ``FitOffer``/``DemandConfig``
    and explicit-officer-knob-always-wins is inherited from ``inventory_rows``, never
    duplicated. ``db_default`` on every column keeps a code-only rollback INSERT-safe.
    """

    is_active = models.BooleanField(default=True, db_default=True)
    sweep_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Warm the board cache and fire the officer problem-set digest on its "
                    "schedule. Off: the board is still viewable on demand, but nothing pings."),
    )
    at_risk_days = models.PositiveIntegerField(
        default=3, db_default=3,
        help_text=_("Flag an order as at-risk when its estimate is within this many days "
                    "(also the stalled-haul age)."),
    )
    commitments_due_days = models.PositiveIntegerField(
        default=7, db_default=7,
        help_text=_("Surface a restock commitment when it is due within this many days."),
    )
    stale_reconcile_days = models.PositiveIntegerField(
        default=14, db_default=14,
        help_text=_("Flag stocked fits not reconciled within this many days as a discrepancy."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("board config")
        verbose_name_plural = _("board configs")

    def __str__(self) -> str:
        return f"BoardConfig #{self.pk}{' active' if self.is_active else ''}"

    @classmethod
    def active(cls) -> BoardConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if cfg is None:
            cfg = cls.objects.create(is_active=True)
        return cfg
