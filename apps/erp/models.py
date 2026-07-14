"""Lightweight industrial ERP (PRD Module O).

Demand-scoped: turns "we need N of X" into claimable build jobs with the BOM and
material readiness worked out, tracks blueprint coverage, and on delivery
updates corp stock and credits the builder. Not a general ERP — it exists to
make doctrine/supply demand executable.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import JSONField, Value
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel

from .messages import render_text


class BuildJob(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        BLOCKED = "blocked", _("Blocked")
        BUILDING = "building", _("Building")
        BUILT = "built", _("Built")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")

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
    # The prose columns stay: they are the English fallback AND the audit record of what the
    # board actually said, and they are what legacy rows (written before the *_key columns
    # landed) render from. Nothing is backfilled — a keyless row degrades to its stored
    # English, never to blank. A pilot-typed ``note`` is human free-text and stays verbatim.
    note = models.CharField(max_length=200, blank=True)
    blocked_reason = models.CharField(
        max_length=200, blank=True, help_text=_("Why a queued job can't start (materials short).")
    )
    # Seam B (see ``messages.py``): the writer of these sentences is never their reader — the
    # board is read by every other pilot, in the language *they* chose — so the prose above can
    # only ever be frozen English. These carry the scaffold key + its plain JSON params so
    # ``*_i18n`` can re-render the sentence under the READER's locale.
    blocked_reason_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    blocked_reason_params = models.JSONField(
        blank=True, default=dict, db_default=Value({}, JSONField())
    )
    note_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    note_params = models.JSONField(blank=True, default=dict, db_default=Value({}, JSONField()))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"build {self.quantity}× {self.output_type_id}"

    # --- read side of Seam B ------------------------------------------------
    # Each resolves its scaffold under the READER's active locale, and falls back to the stored
    # English prose when the row carries no key (every legacy row, and every pilot-typed note)
    # or the key is unknown to this deploy. They can never return blank.
    @property
    def blocked_reason_i18n(self) -> str:
        return render_text(self.blocked_reason_key, self.blocked_reason_params, self.blocked_reason)

    @property
    def note_i18n(self) -> str:
        return render_text(self.note_key, self.note_params, self.note)

    @property
    def is_active(self) -> bool:
        return self.status in (
            self.Status.QUEUED, self.Status.BLOCKED, self.Status.BUILDING, self.Status.BUILT
        )


class Blueprint(TimeStampedModel):
    class Owner(models.TextChoices):
        CORPORATION = "corporation", _("Corporation")
        CHARACTER = "character", _("Character")

    owner_type = models.CharField(max_length=12, choices=Owner.choices, default=Owner.CORPORATION)
    owner_id = models.BigIntegerField(default=0)
    type_id = models.IntegerField(db_index=True, help_text=_("Blueprint type id."))
    product_type_id = models.IntegerField(null=True, blank=True, db_index=True)
    me = models.PositiveSmallIntegerField(default=0)
    te = models.PositiveSmallIntegerField(default=0)
    source = models.CharField(max_length=8, default="manual")
    # ESI provenance (source="esi"): the in-game item id identifies a stack, and
    # quantity distinguishes an original (-1, builds forever) from a copy (-2, with
    # a finite ``runs``). Coverage cares whether a usable BPO/BPC is actually owned.
    item_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    quantity = models.IntegerField(default=-1, help_text=_("ESI: -1 original (BPO), -2 copy (BPC)."))
    runs = models.IntegerField(default=-1, help_text=_("BPC runs remaining; -1 for an original."))
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
        1: _("Manufacturing"), 3: _("TE research"), 4: _("ME research"),
        5: _("Copying"), 7: _("Reverse engineering"), 8: _("Invention"), 9: _("Reactions"),
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
        return self.ACTIVITY_LABELS.get(self.activity_id, _("Activity %(id)s") % {"id": self.activity_id})

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
        1: _("Manufacturing"), 3: _("TE research"), 4: _("ME research"),
        5: _("Copying"), 7: _("Reverse engineering"), 8: _("Invention"), 9: _("Reactions"),
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
        return self.ACTIVITY_LABELS.get(self.activity_id, _("Activity %(id)s") % {"id": self.activity_id})

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
