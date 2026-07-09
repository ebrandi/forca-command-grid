"""Freight service: an officer-tunable rate card and courier contracts.

The rate card holds the courier pricing constants; a single ``discount`` multiplier
(default 0.80) scales every quote, so leadership tune their margin in one place.
Courier contracts are the jobs pilots run for ISK — quoted, posted, claimed,
flown, delivered.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from core.mixins import TimeStampedModel


class ShipClass(models.TextChoices):
    DST = "dst", "Blockade Runner / DST"
    FREIGHTER = "freighter", "Freighter"
    JF = "jf", "Jump Freighter"


class Audience(models.TextChoices):
    PUBLIC = "public", "Public — anyone can get quotes"
    ALLIANCE = "alliance", "Corp & alliance members only"
    CORP = "corp", "Corp members only"
    DISABLED = "disabled", "Disabled"


class RateCard(TimeStampedModel):
    """Pricing constants for the freight calculator. One active row is used.

    Money fields are ISK. Rates are stored at full value; ``discount`` is a price
    multiplier (e.g. 0.80) applied to every quote, so leadership can set their
    margin in one place. ``audience`` controls who may use the service.
    """

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)

    # Who the service is open to (leadership toggles public vs members-only).
    # Default is members-only: corp and registered alliance pilots, not the public.
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.ALLIANCE)

    # Price multiplier applied to the full rate (0.80 = 20% lower).
    discount = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.800"))
    min_reward = models.BigIntegerField(default=4_500_000)

    # Per-warp rates (a warp = jumps + 1).
    dst_rate_per_warp = models.BigIntegerField(default=1_500_000)
    dst_lowsec_rate_per_warp = models.BigIntegerField(default=3_750_000)
    dst_max_m3 = models.BigIntegerField(default=62_500)
    dst_max_collateral = models.BigIntegerField(default=10_000_000_000)

    freighter_rate_per_warp = models.BigIntegerField(default=2_250_000)
    freighter_rate_per_warp_long = models.BigIntegerField(default=2_500_000)
    freighter_long_threshold = models.IntegerField(default=31)  # warps at/over → long rate
    freighter_max_m3 = models.BigIntegerField(default=950_000)
    freighter_max_collateral = models.BigIntegerField(default=5_000_000_000)

    # Jump freighter: flat base + per cyno jump (range hop), priced off the
    # proximity-graph route (apps/logistics/jumps.py), not gate jumps.
    jf_base = models.BigIntegerField(default=200_000_000)
    jf_per_jump = models.BigIntegerField(default=100_000_000)
    jf_max_m3 = models.BigIntegerField(default=360_000)
    jf_max_collateral = models.BigIntegerField(default=50_000_000_000)
    # Assumed Jump Drive Calibration level for the corp's haulers, setting the
    # range used to compute cyno jumps (JDC V → 10 ly). Officer-tunable.
    jf_assumed_jdc = models.PositiveSmallIntegerField(default=5)

    # Rush surcharges (flat, pre-discount).
    rush_fee_hs = models.BigIntegerField(default=50_000_000)
    rush_fee_jf = models.BigIntegerField(default=200_000_000)

    # Days a hauler has to complete a posted contract.
    contract_days = models.IntegerField(default=7)

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"RateCard<{self.name}{' active' if self.is_active else ''}>"


class CourierContract(TimeStampedModel):
    """A hauling job: a customer's items moved A→B for a reward.

    Mirrors an in-game courier contract so a pilot can pick it up, fly it, and
    get paid. ``reward`` and ``breakdown`` are computed from the rate card at
    creation time and frozen, so a later rate change never alters live jobs.
    """

    class Status(models.TextChoices):
        QUOTE = "quote", "Quote"
        OUTSTANDING = "outstanding", "Outstanding"
        IN_PROGRESS = "in_progress", "In progress"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    # Customer the haul is for (free text so external corps/alliances work).
    customer = models.CharField(max_length=120, blank=True)
    contact = models.CharField(max_length=120, blank=True)  # in-game name to contract to

    # Endpoints. ``*_name`` is the specific location label shown to the hauler
    # (station / structure / system); ``*_system_id`` is the system used for
    # routing; ``*_location_kind`` records what the label refers to and
    # ``*_location_id`` the station/structure id (null for a system-level point).
    origin_system_id = models.IntegerField(null=True, blank=True)
    origin_name = models.CharField(max_length=200)
    origin_location_kind = models.CharField(max_length=10, default="system")
    origin_location_id = models.BigIntegerField(null=True, blank=True)
    dest_system_id = models.IntegerField(null=True, blank=True)
    dest_name = models.CharField(max_length=200)
    dest_location_kind = models.CharField(max_length=10, default="system")
    dest_location_id = models.BigIntegerField(null=True, blank=True)

    jumps = models.IntegerField(default=0)
    lowsec_jumps = models.IntegerField(default=0)  # low/null systems crossed (JF pricing)
    sec_band = models.CharField(max_length=10, default="highsec")  # highsec / lowsec / nullsec

    ship_class = models.CharField(max_length=12, choices=ShipClass.choices, default=ShipClass.FREIGHTER)
    volume_m3 = models.FloatField(default=0.0)
    collateral = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    rush = models.BooleanField(default=False)

    reward = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    breakdown = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUOTE)
    notes = models.CharField(max_length=300, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="courier_contracts_created",
    )
    # Who the job is posted under, so a hauler knows whose in-game contract to
    # look for: the poster's own pilot or the corporation they belong to.
    posted_as_kind = models.CharField(max_length=12, default="character")  # character | corporation
    posted_as_id = models.BigIntegerField(null=True, blank=True)
    posted_as_name = models.CharField(max_length=120, blank=True)

    assigned_hauler_character_id = models.BigIntegerField(null=True, blank=True)
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="courier_contracts_hauled",
    )

    class Verification(models.TextChoices):
        UNVERIFIED = "unverified", "Not verified"
        VERIFIED = "verified", "Verified in-game"
        FAILED = "failed", "Failed in-game"

    # ESI cross-check of the actual in-game contract (see contracts_esi.py). A
    # self-reported delivery only earns full haul credit once verified.
    verification_state = models.CharField(
        max_length=12, choices=Verification.choices, default=Verification.UNVERIFIED,
        db_index=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    esi_contract_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    # LOG-1 (3.2): stamped when a pre-deadline reminder DM has been sent, so the sweep sends
    # it at most once; cleared when the haul is released back to the pool.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.origin_name} → {self.dest_name} ({self.get_status_display()})"

    @property
    def reward_per_jump(self) -> float:
        return float(self.reward) / self.jumps if self.jumps else float(self.reward)

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OUTSTANDING


class CorpContract(TimeStampedModel):
    """A snapshot of a corporation contract (ESI corp contracts) for oversight.

    The whole contract picture beyond our own courier service — item exchanges,
    auctions, loans and couriers issued to or by the corp — so officers can see
    what's outstanding, in progress or recently finished. Snapshot-keyed by the
    ESI contract id; rebuilt each sync.
    """

    contract_id = models.BigIntegerField(primary_key=True)
    type = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=24, blank=True, db_index=True)
    issuer_id = models.BigIntegerField(null=True, blank=True)
    issuer_name = models.CharField(max_length=200, blank=True)
    assignee_id = models.BigIntegerField(null=True, blank=True)
    assignee_name = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=255, blank=True)
    price = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    reward = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    volume = models.FloatField(default=0)
    date_issued = models.DateTimeField(null=True, blank=True, db_index=True)
    date_expired = models.DateTimeField(null=True, blank=True)
    date_completed = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_issued"]

    def __str__(self) -> str:
        return self.title or f"Contract {self.contract_id}"

    @property
    def is_open(self) -> bool:
        return self.status in ("outstanding", "in_progress")
