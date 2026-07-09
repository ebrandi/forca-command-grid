"""Lightweight industrial ERP (PRD Module O).

Demand-scoped: turns "we need N of X" into claimable build jobs with the BOM and
material readiness worked out, tracks blueprint coverage, and on delivery
updates corp stock and credits the builder. Not a general ERP — it exists to
make doctrine/supply demand executable.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.mixins import TimeStampedModel


class BuildJob(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        BLOCKED = "blocked", "Blocked"
        BUILDING = "building", "Building"
        BUILT = "built", "Built"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    output_type_id = models.IntegerField(db_index=True)
    quantity = models.IntegerField(default=1)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="build_jobs",
    )
    deliver_to = models.ForeignKey(
        "stockpile.Stockpile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # IND-1 (3.3): the plan line this job was pushed from, so delivery flows back to the plan.
    source_item = models.ForeignKey(
        "industry.IndustryProjectItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="build_jobs",
    )
    due_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    blocked_reason = models.CharField(
        max_length=200, blank=True, help_text="Why a queued job can't start (materials short)."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"build {self.quantity}× {self.output_type_id}"

    @property
    def is_active(self) -> bool:
        return self.status in (
            self.Status.QUEUED, self.Status.BLOCKED, self.Status.BUILDING, self.Status.BUILT
        )


class Blueprint(TimeStampedModel):
    class Owner(models.TextChoices):
        CORPORATION = "corporation", "Corporation"
        CHARACTER = "character", "Character"

    owner_type = models.CharField(max_length=12, choices=Owner.choices, default=Owner.CORPORATION)
    owner_id = models.BigIntegerField(default=0)
    type_id = models.IntegerField(db_index=True, help_text="Blueprint type id.")
    product_type_id = models.IntegerField(null=True, blank=True, db_index=True)
    me = models.PositiveSmallIntegerField(default=0)
    te = models.PositiveSmallIntegerField(default=0)
    source = models.CharField(max_length=8, default="manual")
    # ESI provenance (source="esi"): the in-game item id identifies a stack, and
    # quantity distinguishes an original (-1, builds forever) from a copy (-2, with
    # a finite ``runs``). Coverage cares whether a usable BPO/BPC is actually owned.
    item_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    quantity = models.IntegerField(default=-1, help_text="ESI: -1 original (BPO), -2 copy (BPC).")
    runs = models.IntegerField(default=-1, help_text="BPC runs remaining; -1 for an original.")
    location_id = models.BigIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return f"BP {self.type_id} (ME{self.me})"

    @property
    def is_original(self) -> bool:
        """A BPO (builds indefinitely). A copy is quantity -2 with finite runs."""
        return self.quantity == -1

    @property
    def is_usable(self) -> bool:
        """An original, or a copy with runs left."""
        return self.is_original or self.runs != 0


class CorpIndustryJob(TimeStampedModel):
    """A corp industry job imported from ESI (manufacturing, research, reactions…).

    Snapshot of the corp's in-flight + recently-completed jobs, keyed by the ESI
    ``job_id``. Lets the ERP show what's already in production so officers don't
    double-queue a build that's mid-run.
    """

    # ESI industry activity ids (the ones we surface).
    ACTIVITY_LABELS = {
        1: "Manufacturing", 3: "TE research", 4: "ME research",
        5: "Copying", 7: "Reverse engineering", 8: "Invention", 9: "Reactions",
    }

    job_id = models.BigIntegerField(unique=True)
    installer_id = models.BigIntegerField(db_index=True)
    activity_id = models.PositiveSmallIntegerField(default=1)
    blueprint_type_id = models.IntegerField(db_index=True)
    product_type_id = models.IntegerField(null=True, blank=True, db_index=True)
    runs = models.IntegerField(default=1)
    status = models.CharField(max_length=12, default="active", db_index=True)
    facility_id = models.BigIntegerField(null=True, blank=True)
    location_id = models.BigIntegerField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["end_date"]

    def __str__(self) -> str:
        return f"job {self.job_id} ({self.activity_label})"

    @property
    def activity_label(self) -> str:
        return self.ACTIVITY_LABELS.get(self.activity_id, f"Activity {self.activity_id}")

    @property
    def is_active(self) -> bool:
        return self.status in ("active", "paused")


class CharacterIndustryJob(TimeStampedModel):
    """A *personal* industry job imported from a pilot's own ESI token.

    Same shape as :class:`CorpIndustryJob` but keyed by ``character_id`` (each pilot
    imports their own via the opt-in ``my_industry`` scope). Snapshot-replaced per
    character on each sync. Keeps the ESI-reported ``cost`` so the Job Tracker can
    show what a pilot has actually spent in production.
    """

    ACTIVITY_LABELS = {
        1: "Manufacturing", 3: "TE research", 4: "ME research",
        5: "Copying", 7: "Reverse engineering", 8: "Invention", 9: "Reactions",
    }

    character_id = models.BigIntegerField(db_index=True)
    job_id = models.BigIntegerField(unique=True)
    activity_id = models.PositiveSmallIntegerField(default=1)
    blueprint_type_id = models.IntegerField(db_index=True)
    product_type_id = models.IntegerField(null=True, blank=True, db_index=True)
    runs = models.IntegerField(default=1)
    status = models.CharField(max_length=12, default="active", db_index=True)
    cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    facility_id = models.BigIntegerField(null=True, blank=True)
    location_id = models.BigIntegerField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["end_date"]

    def __str__(self) -> str:
        return f"char {self.character_id} job {self.job_id} ({self.activity_label})"

    @property
    def activity_label(self) -> str:
        return self.ACTIVITY_LABELS.get(self.activity_id, f"Activity {self.activity_id}")

    @property
    def is_active(self) -> bool:
        return self.status in ("active", "paused")


class Delivery(TimeStampedModel):
    job = models.ForeignKey(BuildJob, on_delete=models.CASCADE, related_name="deliveries")
    stockpile = models.ForeignKey(
        "stockpile.Stockpile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    quantity = models.IntegerField(default=0)
    delivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # IND-2 (3.4): the inputs actually decremented from corp stock on this delivery
    # ({type_id: qty}) — the audit trail for material burn-down / reconciliation.
    consumed = models.JSONField(default=dict, blank=True)
