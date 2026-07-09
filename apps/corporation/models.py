"""Corporation Profiles: FORCA + referenced corps/alliances."""
from __future__ import annotations

from django.db import models

from core.mixins import ProvenanceMixin, TimeStampedModel


class EveAlliance(ProvenanceMixin):
    alliance_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=200, blank=True)
    ticker = models.CharField(max_length=10, blank=True)

    def __str__(self) -> str:
        return self.name or str(self.alliance_id)


class PartnerAlliance(ProvenanceMixin):
    """An additional alliance granted access to the corp's alliance services.

    Pilots whose character belongs to one of these alliances are treated, for
    alliance-service access (logistics / buyback / store), exactly like pilots of
    the corp's own alliance — they may browse and use the services as clients.
    Managed from the Django admin. The corp's own alliance always has access and
    does not need to be listed here.
    """

    alliance_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(
        max_length=200, blank=True, help_text="Optional label; the id is what grants access."
    )
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "alliance_id"]
        verbose_name = "partner alliance"
        verbose_name_plural = "partner alliances"

    def __str__(self) -> str:
        label = self.name or str(self.alliance_id)
        return label if self.active else f"{label} (inactive)"


class FriendlyCorporation(ProvenanceMixin):
    """An additional corporation granted access to the corp's alliance services.

    Same idea as :class:`PartnerAlliance` but keyed on the corporation id, so a
    friendly corp that is NOT in a partner alliance can still be admitted to the
    logistics / buyback / store services. Managed from the Access-governance console
    page. The home corp always has access and does not need listing here.
    """

    corporation_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(
        max_length=200, blank=True, help_text="Optional label; the id is what grants access."
    )
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "corporation_id"]
        verbose_name = "friendly corporation"
        verbose_name_plural = "friendly corporations"

    def __str__(self) -> str:
        label = self.name or str(self.corporation_id)
        return label if self.active else f"{label} (inactive)"


class EveName(models.Model):
    """Resolved name for any EVE entity id (character/corporation/alliance/…).

    Populated in the background via ESI /universe/names/ so the UI can show
    real names for ids that aren't otherwise in our tables (e.g. enemy pilots).
    """

    entity_id = models.BigIntegerField(primary_key=True)
    category = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=200)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class CorpMember(ProvenanceMixin):
    """A corporation member as seen through the Director member-tracking feed.

    Mirrors ESI ``/corporations/{id}/membertracking/`` so leadership can see who
    is in the corp, where/when they were last active, and — most importantly —
    whether that pilot has registered in Command Grid (linked a character and
    provided an ESI token). Names are resolved best-effort via /universe/names/.
    """

    character_id = models.BigIntegerField(primary_key=True)
    corporation_id = models.BigIntegerField(db_index=True)
    name = models.CharField(max_length=200, blank=True)
    location_id = models.BigIntegerField(null=True, blank=True)
    ship_type_id = models.IntegerField(null=True, blank=True)
    base_id = models.BigIntegerField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    logon_date = models.DateTimeField(null=True, blank=True)
    logoff_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name or str(self.character_id)


class EveCorporation(ProvenanceMixin):
    corporation_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=200, blank=True)
    ticker = models.CharField(max_length=10, blank=True)
    alliance = models.ForeignKey(
        EveAlliance, on_delete=models.SET_NULL, null=True, blank=True, related_name="corporations"
    )
    member_count = models.IntegerField(null=True, blank=True)
    ceo_id = models.BigIntegerField(null=True, blank=True)
    is_home_corp = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.name or str(self.corporation_id)


class CorpWalletDivision(ProvenanceMixin):
    """Current balance of one of the corporation's seven wallet divisions."""

    division = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=120, blank=True)
    balance = models.DecimalField(max_digits=24, decimal_places=2, default=0)

    class Meta:
        ordering = ["division"]

    def __str__(self) -> str:
        return self.name or f"Division {self.division}"


class CorpWalletJournalEntry(models.Model):
    """One immutable corp wallet journal line (ESI wallet journal).

    De-duplicated by the ESI entry id, so re-syncs never double-count. Powers the
    finance page and the member ISK ledger (donations / tax received).
    """

    entry_id = models.BigIntegerField(primary_key=True)
    division = models.IntegerField(db_index=True)
    date = models.DateTimeField(db_index=True)
    ref_type = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    first_party_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    second_party_id = models.BigIntegerField(null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    tax = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-date"]
        indexes = [
            # The guaranteed-buyback ESI reconcile and the member ISK ledger filter by
            # recipient (second_party_id) over a date range; without this the most
            # selective predicate can't drive the query. Composite (party, date) lets a
            # single index serve both the equality and the range/order.
            models.Index(fields=["second_party_id", "date"], name="cwje_secondparty_date_idx"),
        ]


class Contact(ProvenanceMixin):
    """A corp contact with a standing (blue/red list), synced from ESI.

    De-duplicated by the contact's entity id. ``standing`` runs -10..+10; the name is
    resolved separately via EveName so the standings page reads in plain language.
    """

    class ContactType(models.TextChoices):
        CHARACTER = "character", "Character"
        CORPORATION = "corporation", "Corporation"
        ALLIANCE = "alliance", "Alliance"
        FACTION = "faction", "Faction"

    contact_id = models.BigIntegerField(primary_key=True)
    contact_type = models.CharField(max_length=12, choices=ContactType.choices)
    standing = models.FloatField(default=0.0)
    label = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-standing", "name"]

    def __str__(self) -> str:
        return f"{self.name or self.contact_id} ({self.standing:+.1f})"


class CorpStructure(ProvenanceMixin):
    """A corp-owned Upwell structure (ESI corporation structures).

    The daily-driver director board: fuel remaining, online/low-power state, and
    any reinforcement timer. Snapshot-keyed by ``structure_id`` so re-syncs are
    idempotent; a structure that drops off the corp's list is pruned.
    """

    structure_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=200, blank=True)
    type_id = models.IntegerField(db_index=True)
    type_name = models.CharField(max_length=120, blank=True)
    system_id = models.BigIntegerField(null=True, blank=True)
    system_name = models.CharField(max_length=120, blank=True)
    # ESI structure state, e.g. shield_vulnerable / armor_reinforce / hull_reinforce
    # / anchoring / online_deprecated / fitting_invulnerable / unanchored.
    state = models.CharField(max_length=32, blank=True, db_index=True)
    fuel_expires = models.DateTimeField(null=True, blank=True, db_index=True)
    state_timer_start = models.DateTimeField(null=True, blank=True)
    state_timer_end = models.DateTimeField(null=True, blank=True)
    unanchors_at = models.DateTimeField(null=True, blank=True)
    reinforce_hour = models.PositiveSmallIntegerField(null=True, blank=True)
    services = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["fuel_expires", "name"]

    def __str__(self) -> str:
        return self.name or f"Structure {self.structure_id}"

    @property
    def fuel_days_left(self) -> float | None:
        if not self.fuel_expires:
            return None
        from django.utils import timezone

        return max(0.0, (self.fuel_expires - timezone.now()).total_seconds() / 86400)

    @property
    def is_low_fuel(self) -> bool:
        """Under the leadership-configured "refuel now" threshold (default 3 days)."""
        days = self.fuel_days_left
        if days is None:
            return False
        fuel_days, _ = StructureAlertConfig.thresholds()
        return days < fuel_days

    @property
    def is_out_of_fuel(self) -> bool:
        days = self.fuel_days_left
        return days is not None and days <= 0

    @property
    def is_reinforced(self) -> bool:
        from django.utils import timezone

        return bool(self.state_timer_end and self.state_timer_end > timezone.now())


class StructureAlertConfig(TimeStampedModel):
    """Leadership-tunable thresholds for the structure-fuel / sov-ADM alerts (CORP-3).

    Singleton (via ``active()``). Drives both the board's "low fuel" / "soft" flags and
    the deduped infrastructure alert, replacing the previously hard-coded 3-day / 3.0
    constants — e.g. a staging Keepstar can warn at 5 days.
    """

    is_active = models.BooleanField(default=True)
    fuel_alert_days = models.PositiveSmallIntegerField(
        default=3, help_text="A structure with fewer than this many days of fuel is low and alerts.",
    )
    adm_alert_floor = models.FloatField(
        default=3.0, help_text="A sovereignty system with an ADM below this is soft and alerts.",
    )

    _CACHE_KEY = "corp:structure_alert_cfg"
    _DEFAULT_FUEL_DAYS = 3.0
    _DEFAULT_ADM_FLOOR = 3.0

    def __str__(self) -> str:
        return f"StructureAlertConfig(fuel<{self.fuel_alert_days}d, adm<{self.adm_alert_floor})"

    @classmethod
    def active(cls) -> StructureAlertConfig:
        """The singleton for editing on the console (seeded by migration 0010; created on
        demand only if that row was somehow removed)."""
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        return cfg or cls.objects.create(is_active=True)

    @classmethod
    def thresholds(cls) -> tuple[float, float]:
        """``(fuel_alert_days, adm_alert_floor)``, briefly cached so the board's per-row
        ``is_low_fuel`` / ``is_soft`` checks don't each hit the DB. Read-only: falls back
        to the defaults if no row exists rather than writing one on a GET/board render."""
        from django.core.cache import cache

        cached = cache.get(cls._CACHE_KEY)
        if cached is None:
            c = cls.objects.filter(is_active=True).order_by("-updated_at").first()
            cached = (
                (float(c.fuel_alert_days), float(c.adm_alert_floor)) if c
                else (cls._DEFAULT_FUEL_DAYS, cls._DEFAULT_ADM_FLOOR)
            )
            cache.set(cls._CACHE_KEY, cached, 300)
        return cached

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache

        cache.delete(self._CACHE_KEY)


class MoonExtraction(models.Model):
    """A scheduled moon-mining extraction (ESI corporation mining extractions).

    ``chunk_arrival`` is when the rock is ready to fracture — the timer miners care
    about. De-duplicated per structure + arrival so re-syncs are idempotent.
    """

    structure_id = models.BigIntegerField(db_index=True)
    moon_id = models.BigIntegerField(null=True, blank=True)
    moon_name = models.CharField(max_length=200, blank=True)
    structure_name = models.CharField(max_length=200, blank=True)
    extraction_start = models.DateTimeField(null=True, blank=True)
    chunk_arrival = models.DateTimeField(db_index=True)
    natural_decay = models.DateTimeField(null=True, blank=True)
    # MIN-3 (3.13): offset-hours (e.g. [24, 1]) we've already sent a chunk-arrival reminder
    # for, so each reminder fires at most once per extraction.
    reminders_sent = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = ("structure_id", "chunk_arrival")
        ordering = ["chunk_arrival"]

    def __str__(self) -> str:
        return f"{self.moon_name or self.moon_id} @ {self.chunk_arrival:%Y-%m-%d %H:%M}"


# --- access-cache invalidation ------------------------------------------------
# PartnerAlliance / FriendlyCorporation drive the cached access sets read on the hot
# path (apps.corporation.access). They change only via the Director access-governance
# console, so invalidate the cache on any write to either — catches every write path,
# not just the console view.
from django.db.models.signals import post_delete, post_save  # noqa: E402
from django.dispatch import receiver  # noqa: E402


@receiver(post_save, sender=PartnerAlliance)
@receiver(post_delete, sender=PartnerAlliance)
@receiver(post_save, sender=FriendlyCorporation)
@receiver(post_delete, sender=FriendlyCorporation)
def _invalidate_access_cache(sender, instance, **kwargs):
    from .access import invalidate_access_cache

    invalidate_access_cache()


@receiver(post_save, sender=EveCorporation)
def _invalidate_home_corp_cache(sender, instance, **kwargs):
    """The cached home-corp name + alliance-id set derive from the home EveCorporation row;
    invalidate them when it is (re)synced so a rename/alliance change shows immediately.
    Gated on the home corp id so a routine sync of other corps costs one int compare."""
    from django.conf import settings

    if instance.corporation_id == getattr(settings, "FORCA_HOME_CORP_ID", 0):
        from .access import invalidate_access_cache

        invalidate_access_cache()
