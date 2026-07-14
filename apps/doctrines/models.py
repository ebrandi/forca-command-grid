"""Doctrine Management: library, fits, requirements, skill requirements."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel

from . import xml_parser


class DoctrineCategory(models.Model):
    key = models.SlugField(max_length=32, unique=True)
    label = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "label"]
        verbose_name_plural = _("doctrine categories")

    def __str__(self) -> str:
        return self.label


class Doctrine(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        RETIRED = "retired", _("Retired")

    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        DoctrineCategory, on_delete=models.SET_NULL, null=True, related_name="doctrines"
    )
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    is_public_preview = models.BooleanField(default=False)
    priority = models.IntegerField(default=0, help_text=_("Corp-need weight for skill planning."))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self) -> str:
        return self.name


class DoctrineFit(TimeStampedModel):
    doctrine = models.ForeignKey(Doctrine, on_delete=models.CASCADE, related_name="fits")
    name = models.CharField(max_length=200)
    ship_type_id = models.IntegerField()
    role = models.CharField(max_length=40, blank=True)
    eft_text = models.TextField(blank=True)
    # Normalised: list of {"type_id": int, "quantity": int, "slot": str}
    modules = models.JSONField(default=list, blank=True)
    is_cheap_alt = models.BooleanField(default=False)
    estimated_cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    class Meta:
        # Stable insertion order so same-hull, same-score fit matching (4.2) and the
        # priority fast-path are fully deterministic instead of DB-row-order-dependent.
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.doctrine.name} · {self.name}"


class DoctrineRequirement(models.Model):
    class Kind(models.TextChoices):
        # Implant/Booster/Rig/Ammo are EVE item categories — left English on purpose.
        IMPLANT = "implant", "Implant"
        BOOSTER = "booster", "Booster"
        RIG = "rig", "Rig"
        AMMO = "ammo", "Ammo"
        NOTE = "note", _("Note")

    fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="requirements")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    type_id = models.IntegerField(null=True, blank=True)
    text = models.TextField(blank=True)
    is_recommended = models.BooleanField(default=True)


class SkillRequirement(models.Model):
    class DerivedFrom(models.TextChoices):
        AUTO_DOGMA = "auto_dogma", _("Auto (dogma)")
        MANUAL = "manual", _("Manual override")

    fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="skill_requirements")
    skill_type_id = models.IntegerField()
    min_level = models.PositiveSmallIntegerField(default=1)
    optimal_level = models.PositiveSmallIntegerField(default=1)
    derived_from = models.CharField(
        max_length=12, choices=DerivedFrom.choices, default=DerivedFrom.AUTO_DOGMA
    )

    class Meta:
        unique_together = ("fit", "skill_type_id")


class DoctrineImportBatch(TimeStampedModel):
    """Server-side staging for one EVE-client XML doctrine import.

    Holds the parsed + classified fittings between the preview and the confirmed
    commit so the browser never carries — nor can tamper with — the authoritative
    fit data. The commit re-reads ``payload`` from here and applies only the
    owner's per-fitting decisions (skip / rename / replace). Nothing here touches
    the Doctrine tables until commit; the batch is disposable staging, owner-scoped
    (an IDOR guard, never another director's batch) and pruned after a short TTL.
    """

    class Status(models.TextChoices):
        PREVIEW = "preview", _("Awaiting review")
        COMMITTED = "committed", _("Imported")
        EXPIRED = "expired", _("Expired")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="doctrine_import_batches",
    )
    # Sanitised original filename — display only, never used to open/serve a file.
    source_filename = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PREVIEW)
    # Server-authoritative parsed + classified fittings; the commit trusts THIS,
    # not the form. Each entry: {index, name, ship_name, ship_type_id, status,
    # modules:[{type_id,quantity,slot,name}], hardware:[…display…], reasons:[…],
    # warnings:[…], existing:{…}}.
    payload = models.JSONField(default=list, blank=True)
    # Totals by classification at parse time (summary cards + audit).
    counts = models.JSONField(default=dict, blank=True)
    # Outcome of the commit (created/skipped/renamed/replaced/rejected + notes).
    result = models.JSONField(default=dict, blank=True)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = _("doctrine import batches")

    def __str__(self) -> str:
        return f"XML import #{self.pk} ({self.get_status_display()})"


class DoctrineImportConfig(TimeStampedModel):
    """Leadership-tunable settings for the EVE-XML doctrine importer.

    One active row (see :meth:`active`). Today it holds the only knob leaders have
    asked for — how many fittings a single upload may contain — but it is the
    natural home for any future import limits.
    """

    is_active = models.BooleanField(default=True)
    max_fittings_per_import = models.PositiveIntegerField(
        default=xml_parser.DEFAULT_MAX_FITTINGS,
        help_text=_(
            "How many fittings one XML upload may contain (1–%(ceiling)s). "
            "%(ceiling)s is the hard safety maximum; set it there to effectively "
            "remove the limit."
        ) % {"ceiling": xml_parser.MAX_FITTINGS_CEILING},
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("doctrine import config")
        verbose_name_plural = _("doctrine import config")

    def __str__(self) -> str:
        return f"DoctrineImportConfig #{self.pk} (max {self.max_fittings_per_import})"

    @classmethod
    def active(cls) -> DoctrineImportConfig:
        config = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if config is None:
            config = cls.objects.create(is_active=True)
        return config

    def effective_max_fittings(self) -> int:
        """The configured limit, clamped into the safe range (never above the
        ceiling, never below 1)."""
        return xml_parser.clamp_max_fittings(self.max_fittings_per_import)


# How many doctrines/ships the browse pages show per page, and the bounds a leader
# may set it to (kept sane so a page never renders thousands of cards at once).
DEFAULT_PER_PAGE = 24
MIN_PER_PAGE = 6
MAX_PER_PAGE = 200


def clamp_per_page(value: int | None) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    return max(MIN_PER_PAGE, min(value, MAX_PER_PAGE))


class DoctrineDisplayConfig(TimeStampedModel):
    """Leadership-tunable display settings for the pilot-facing doctrine pages.

    One active row (see :meth:`active`). Currently just the page size shared by the
    Doctrines library and the Shipyard, so a corp with hundreds of doctrines paginates
    instead of rendering them all at once.
    """

    is_active = models.BooleanField(default=True)
    per_page = models.PositiveIntegerField(
        default=DEFAULT_PER_PAGE,
        help_text=_(
            "Doctrines/ships shown per page on the Doctrines and Shipyard pages "
            "(%(min)s–%(max)s)."
        ) % {"min": MIN_PER_PAGE, "max": MAX_PER_PAGE},
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("doctrine display config")
        verbose_name_plural = _("doctrine display config")

    def __str__(self) -> str:
        return f"DoctrineDisplayConfig #{self.pk} ({self.per_page}/page)"

    @classmethod
    def active(cls) -> DoctrineDisplayConfig:
        config = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if config is None:
            config = cls.objects.create(is_active=True)
        return config

    def effective_per_page(self) -> int:
        return clamp_per_page(self.per_page)
