"""Killboard: killmails, participants, items, battle reports, watchlist."""
from __future__ import annotations

import hashlib
import logging
import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.doctrines.models import DoctrineFit
from core.mixins import ProvenanceMixin, TimeStampedModel

from . import ranks_i18n


class SecBand(models.TextChoices):
    HIGHSEC = "highsec", "Highsec"
    LOWSEC = "lowsec", "Lowsec"
    NULLSEC = "nullsec", "Nullsec"
    WORMHOLE = "wh", "Wormhole"
    ABYSSAL = "abyssal", "Abyssal"
    POCHVEN = "pochven", "Pochven"
    UNKNOWN = "unknown", _("Unknown")


class Killmail(ProvenanceMixin):
    class HomeRole(models.TextChoices):
        VICTIM = "victim", "Victim"
        ATTACKER = "attacker", "Attacker"
        NONE = "none", "None"

    killmail_id = models.BigIntegerField(primary_key=True)
    killmail_hash = models.CharField(max_length=64)
    killmail_time = models.DateTimeField(db_index=True)
    solar_system_id = models.IntegerField(db_index=True)
    region_id = models.IntegerField(null=True, blank=True, db_index=True)
    moon_id = models.IntegerField(null=True, blank=True)
    war_id = models.IntegerField(null=True, blank=True)

    victim_character_id = models.BigIntegerField(null=True, blank=True)
    victim_corporation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    victim_alliance_id = models.BigIntegerField(null=True, blank=True)
    victim_faction_id = models.IntegerField(null=True, blank=True)
    victim_ship_type_id = models.IntegerField()
    damage_taken = models.IntegerField(default=0)

    # derived
    total_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    destroyed_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    dropped_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    fitted_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    points = models.IntegerField(default=1)
    is_solo = models.BooleanField(default=False)
    is_npc = models.BooleanField(default=False)
    is_awox = models.BooleanField(default=False)
    sec_band = models.CharField(max_length=10, choices=SecBand.choices, default=SecBand.UNKNOWN)

    # Not independently indexed: the (involves_home_corp, home_corp_role, killmail_time DESC)
    # composite (km_home_role_time_idx) has it as its leading column, so it already serves
    # every involves_home_corp filter — a standalone index would just be write overhead.
    involves_home_corp = models.BooleanField(default=False)
    home_corp_role = models.CharField(max_length=10, choices=HomeRole.choices, default=HomeRole.NONE)
    doctrine_fit = models.ForeignKey(
        DoctrineFit, on_delete=models.SET_NULL, null=True, blank=True, related_name="tagged_kills"
    )
    valuation_version = models.IntegerField(default=1)
    points_version = models.IntegerField(default=1)

    class Meta:
        ordering = ["-killmail_time"]
        indexes = [
            # The public killfeed filters on involves_home_corp (+ optional role)
            # and orders by killmail_time desc; this composite serves that exact
            # shape and the analytics _pvp() scans, instead of bitmap-and-ing
            # separate single-column indexes on a table heading past 100k rows.
            models.Index(
                fields=["involves_home_corp", "home_corp_role", "-killmail_time"],
                name="km_home_role_time_idx",
            ),
            # "Top ships" boards group losses by victim ship; pilot losses filter
            # by victim character — both unindexed before and scanned per board.
            models.Index(fields=["victim_ship_type_id"], name="km_victim_ship_idx"),
            models.Index(fields=["victim_character_id"], name="km_victim_char_idx"),
            # The killfeed's victim-side alliance filter (KB-03) keys on this
            # otherwise-unindexed column over a 100k+ row table.
            models.Index(fields=["victim_alliance_id"], name="km_victim_alliance_idx"),
            # "Most valuable kills" ORDER BY total_value DESC on cold windows (the
            # "all" tab, historical years) had no supporting index and sorted the
            # whole PvP-kill set. This partial composite lets Postgres seek by
            # home_corp_role and read total_value in order, over just the home-corp
            # non-NPC kills the query ever touches.
            models.Index(
                fields=["home_corp_role", "-total_value"],
                name="km_role_value_idx",
                condition=models.Q(involves_home_corp=True, is_npc=False),
            ),
        ]

    def __str__(self) -> str:
        return f"killmail {self.killmail_id}"


class KillmailParticipant(models.Model):
    class Role(models.TextChoices):
        VICTIM = "victim", "Victim"
        ATTACKER = "attacker", "Attacker"

    # db_index=False: the unique_together (killmail_id, role, seq) index already serves every
    # WHERE killmail_id=? / FK join, so the auto FK index is redundant write overhead on this
    # 5.5M-row append-heavy table. See handbooks/reference/database.md (R1).
    killmail = models.ForeignKey(
        Killmail, on_delete=models.CASCADE, related_name="participants", db_index=False
    )
    role = models.CharField(max_length=8, choices=Role.choices)
    seq = models.IntegerField(default=0)
    character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    corporation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    faction_id = models.IntegerField(null=True, blank=True)
    ship_type_id = models.IntegerField(null=True, blank=True)
    weapon_type_id = models.IntegerField(null=True, blank=True)
    damage_done = models.IntegerField(default=0)
    final_blow = models.BooleanField(default=False)
    security_status = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = ("killmail", "role", "seq")
        indexes = [
            # The leaderboard kill-rows and doctrine-compliance queries filter
            # attacker rows by home corporation and character; this composite
            # serves that join over ~1M participant rows.
            models.Index(
                fields=["role", "corporation_id", "character_id"],
                name="kp_role_corp_char_idx",
            ),
            # The killfeed's attacker-side alliance filter (KB-03) resolves a
            # killmail_id subquery over attacker participants keyed by alliance.
            models.Index(fields=["role", "alliance_id"], name="kp_role_alliance_idx"),
        ]


class KillmailItem(models.Model):
    # db_index=False: the unique_together (killmail_id, idx) index already serves every
    # WHERE killmail_id=? / FK join — the auto FK index is redundant on this 3.5M-row table.
    # See handbooks/reference/database.md (R2).
    killmail = models.ForeignKey(
        Killmail, on_delete=models.CASCADE, related_name="items", db_index=False
    )
    idx = models.IntegerField()
    parent_idx = models.IntegerField(null=True, blank=True)
    item_type_id = models.IntegerField()
    flag = models.IntegerField(default=0)
    singleton = models.IntegerField(default=0)
    quantity_dropped = models.BigIntegerField(null=True, blank=True)
    quantity_destroyed = models.BigIntegerField(null=True, blank=True)
    unit_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ("killmail", "idx")


class FitDeviation(TimeStampedModel):
    """How a doctrine-tagged loss's actual fit differed from the canonical doctrine
    fit (KB-14 / SRP-7), computed at ingest.

    ``missing`` = modules in the doctrine fit but not on the loss; ``extra`` =
    modules on the loss but not in the doctrine fit — each a list of
    ``{"type_id": int, "quantity": int}``. Sensitive: a pilot's individual
    deviations are shown only to themselves and officers (never to peers).
    """

    killmail = models.OneToOneField(Killmail, on_delete=models.CASCADE, related_name="fit_deviation")
    doctrine_fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="loss_deviations")
    missing = models.JSONField(default=list)
    extra = models.JSONField(default=list)

    @property
    def is_clean(self) -> bool:
        return not self.missing and not self.extra


class KillmailComment(models.Model):
    """A member's short note on a killmail (KB-22).

    Corp-private discussion on a kill/loss — members post, the author or an officer
    removes. Attribution is snapshotted (``author_name`` / ``author_character_id``) at
    post time so a later pilot-switch or account change doesn't rewrite history, while the
    ``author`` FK is kept for the delete-permission check.
    """

    killmail = models.ForeignKey(Killmail, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    author_name = models.CharField(max_length=120, blank=True)
    author_character_id = models.BigIntegerField(null=True, blank=True)
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"comment on {self.killmail_id} by {self.author_name}"


class BattleReport(TimeStampedModel):
    title = models.CharField(max_length=200, blank=True)
    system_ids = models.JSONField(default=list)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    sides = models.JSONField(default=dict)
    isk_destroyed_by_side = models.JSONField(default=dict)
    ship_breakdown = models.JSONField(default=dict)
    is_public = models.BooleanField(default=False)
    killmails = models.ManyToManyField(Killmail, related_name="battle_reports", blank=True)


class CombatMetric(ProvenanceMixin):
    class EntityType(models.TextChoices):
        CHARACTER = "character", "Character"
        CORPORATION = "corporation", "Corporation"
        ALLIANCE = "alliance", "Alliance"
        SHIP = "ship", "Ship"
        SYSTEM = "system", "System"

    entity_type = models.CharField(max_length=12, choices=EntityType.choices)
    entity_id = models.BigIntegerField()
    window = models.CharField(max_length=8, default="all")  # 7d / 30d / all
    kills = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    isk_destroyed = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    isk_lost = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    points = models.IntegerField(default=0)
    solo_kills = models.IntegerField(default=0)
    final_blows = models.IntegerField(default=0)
    danger_ratio = models.FloatField(default=0.0)
    gang_ratio = models.FloatField(default=0.0)
    avg_gang_size = models.FloatField(default=0.0)
    top_ships = models.JSONField(default=list, blank=True)
    top_systems = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = ("entity_type", "entity_id", "window")


class Watchlist(TimeStampedModel):
    name = models.CharField(max_length=200)
    purpose = models.CharField(max_length=200, blank=True)
    # 4.4: opt-in tripwire alerts when a watched entity appears on a fresh killmail. Off by
    # default (a watchlist is a static reference list unless leadership arms its alerts).
    alerts_enabled = models.BooleanField(default=False)


class WatchlistEntry(models.Model):
    class EntityType(models.TextChoices):
        CHARACTER = "character", _("Character")
        CORPORATION = "corporation", _("Corporation")
        ALLIANCE = "alliance", _("Alliance")

    watchlist = models.ForeignKey(Watchlist, on_delete=models.CASCADE, related_name="entries")
    entity_type = models.CharField(max_length=12, choices=EntityType.choices)
    entity_id = models.BigIntegerField()
    note = models.CharField(max_length=200, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    # 4.4: last time we fired an activity tripwire for this entry, so a still-active hostile
    # isn't re-alerted every sweep — only on a fresh activation after the cooldown.
    last_alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("watchlist", "entity_type", "entity_id")


class KillFeedConfig(TimeStampedModel):
    """Leadership-tunable rules for posting killmails to Discord (a singleton).

    A value of 0 on either threshold turns that direction off. Defaults are
    conservative (only sizeable kills/losses) so enabling it doesn't flood the
    channel.
    """

    enabled = models.BooleanField(default=False)
    min_loss_value = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("100000000"),
        help_text=_("Post a corp loss when its value is at least this (0 = off)."),
    )
    min_kill_value = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("500000000"),
        help_text=_("Post a corp kill when its value is at least this (0 = off)."),
    )
    # KB-24 rule engine: additional require/exclude clauses, AND-combined with the ISK floor
    # above. Every field defaults to "off/no filter" so an existing feed is unchanged until an
    # officer configures rules.
    exclude_npc = models.BooleanField(
        default=False, help_text=_("Skip kills with no player attackers (NPC/ratting deaths).")
    )
    exclude_awox = models.BooleanField(
        default=False, help_text=_("Skip awox kills (a corp member on the victim's own corp).")
    )
    require_solo = models.BooleanField(default=False, help_text=_("Post solo kills only."))
    min_attackers = models.PositiveIntegerField(
        default=0, help_text=_("Require at least this many attackers (0 = off).")
    )
    max_attackers = models.PositiveIntegerField(
        default=0, help_text=_("Require at most this many attackers (0 = off).")
    )
    sec_bands = models.JSONField(
        default=list, blank=True,
        help_text=_("Only these security bands (empty = all)."),
    )
    ship_classes = models.JSONField(
        default=list, blank=True,
        help_text=_("Only these victim ship classes (empty = all)."),
    )
    max_jumps_from_staging = models.PositiveIntegerField(
        default=0, help_text=_("Only within this many jumps of the staging system (0 = off)."),
    )
    losses_deviated_only = models.BooleanField(
        default=False, help_text=_("For losses, post only those that deviated from doctrine."),
    )

    @classmethod
    def load(cls) -> KillFeedConfig:
        return cls.objects.first() or cls.objects.create()


class KillFeedPing(models.Model):
    """Dedup record: a killmail already posted to the Discord kill feed."""

    killmail_id = models.BigIntegerField(primary_key=True)
    posted_at = models.DateTimeField(auto_now_add=True)


class NewbroConfig(TimeStampedModel):
    """Leadership settings for how new pilots are framed (a singleton).

    The kill/loss "danger" read shows "Snuggly" for anyone whose balance is
    kill-light — demoralising for a pilot who simply hasn't fought yet. When
    ``soften_danger_label`` is on, a pilot with fewer than ``soften_below_events``
    total kills+losses gets a gentler "Learning" label instead of "Snuggly".
    """

    soften_danger_label = models.BooleanField(
        default=True,
        help_text=_("Show 'Learning' instead of 'Snuggly' for pilots below the activity floor."),
    )
    soften_below_events = models.PositiveIntegerField(
        default=20,
        help_text=_("Total kills + losses below which the danger label is softened."),
    )

    @classmethod
    def load(cls) -> NewbroConfig:
        return cls.objects.first() or cls.objects.create()


class PilotMilestone(models.Model):
    """A new pilot's 'first' combat achievement — recorded once, celebrated once.

    Reachable early wins (first kill, first solo, first final blow) that retain a
    newbro before they can climb the prestige ladder. Recorded for every home-corp
    pilot's history (display), but a Pingboard celebration only fires for a *recently*
    achieved first, so enabling the feature never back-congratulates a veteran for a
    kill from years ago (future-only notifications).
    """

    class Kind(models.TextChoices):
        FIRST_KILL = "first_kill", _("First kill")
        FIRST_SOLO = "first_solo", _("First solo kill")
        FIRST_FINAL_BLOW = "first_final_blow", _("First final blow")

    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=128, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    achieved_at = models.DateTimeField()
    killmail_id = models.BigIntegerField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-achieved_at"]
        constraints = [
            models.UniqueConstraint(fields=["character_id", "kind"], name="uniq_pilot_milestone"),
        ]

    def __str__(self) -> str:
        return f"{self.character_name or self.character_id}: {self.get_kind_display()}"


# --------------------------------------------------------------------------- #
#  Combat rank titles + progression rewards
# --------------------------------------------------------------------------- #
class RankMetric(models.TextChoices):
    """The stat a rank threshold is measured against.

    Only ``KILLS`` is wired into the live rank calculation today (the historical
    default, so nothing changes for existing pilots). The others are declared so
    the ladder is forward-compatible — a future release can rank on solo kills,
    final blows, points, ISK destroyed or active days without a schema change.
    """

    KILLS = "kills", _("All-time PvP kills")
    SOLO_KILLS = "solo_kills", _("Solo kills")
    FINAL_BLOWS = "final_blows", _("Final blows")
    POINTS = "points", _("Points")
    ISK_DESTROYED = "isk_destroyed", _("ISK destroyed")
    ACTIVE_DAYS = "active_days", _("Active days")


class RewardType(models.TextChoices):
    NONE = "none", _("No reward")
    ISK = "isk", "ISK"
    PLEX = "plex", "PLEX"
    ITEM = "item", _("Item")
    MANUAL = "manual", _("Manual / other")


class CombatRankTitle(TimeStampedModel):
    """A configurable rung on the combat rank ladder.

    Replaces the old hard-coded ``leaderboards.RANK_LADDER`` as the source of truth
    (that list survives as the seed + an empty-table fallback, so the rank system
    keeps working even before the seed migration runs). Leaders manage the ladder
    from the Admin Console; ``rank_service.active_ladder`` reads the active rows,
    cached, and ``combat_rank`` maps a kill count onto them.
    """

    name = models.CharField(max_length=64, help_text=_("The title a pilot earns, e.g. “Line Pilot”."))
    metric = models.CharField(
        max_length=16, choices=RankMetric.choices, default=RankMetric.KILLS,
        help_text=_("Which stat the threshold measures. Only “All-time PvP kills” is live today."),
    )
    min_kills = models.PositiveIntegerField(
        default=0,
        help_text=_("Minimum value of the metric to hold this title (0 = the entry-level rank)."),
    )
    description = models.CharField(max_length=200, blank=True)
    badge_icon = models.CharField(
        max_length=32, blank=True,
        help_text=_("Optional icon symbol id from the sprite sheet (e.g. “i-trophy”)."),
    )
    color_class = models.CharField(
        max_length=32, default="text-faint",
        help_text=_("Tailwind text-colour token for the title (e.g. “text-gold”)."),
    )
    sort_order = models.PositiveIntegerField(default=0, help_text=_("Display order (low → high)."))
    is_active = models.BooleanField(
        default=True, help_text=_("Inactive ranks are excluded from the live ladder.")
    )
    is_visible = models.BooleanField(
        default=True, help_text=_("Whether pilots see this rung on the public ladder.")
    )
    grants_reward = models.BooleanField(
        default=False, help_text=_("Whether reaching this rank (after baseline) creates a reward.")
    )
    reward_type = models.CharField(max_length=8, choices=RewardType.choices, default=RewardType.NONE)
    reward_amount = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("ISK amount, or PLEX quantity, per pilot reaching this rank."),
    )
    reward_item_type_id = models.IntegerField(
        null=True, blank=True, help_text=_("For item rewards: the EVE type id."),
    )
    reward_notes = models.CharField(max_length=200, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["metric", "min_kills"]
        constraints = [
            # Thresholds must be unique per metric — enforces the "unique & ascending"
            # ladder rule (ascending is validated in the admin form + sort_order).
            models.UniqueConstraint(fields=["metric", "min_kills"], name="uniq_rank_metric_threshold"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.min_kills}+)"

    @property
    def name_i18n(self) -> str:
        """``name`` for display: the seeded built-in title translated, else verbatim.

        The column keeps canonical English (a lazy proxy would be frozen to str on save) —
        see :mod:`apps.killboard.ranks_i18n`.
        """
        return ranks_i18n.rank_title_for(self.name)

    @property
    def description_i18n(self) -> str:
        """``description`` for display: the seeded built-in text translated, else verbatim."""
        return ranks_i18n.rank_description_for(self.description)

    @property
    def rewards_configured(self) -> bool:
        """A reward-granting rank whose payout is actually set up."""
        if not self.grants_reward or self.reward_type == RewardType.NONE:
            return False
        if self.reward_type == RewardType.ITEM:
            return bool(self.reward_item_type_id)
        if self.reward_type == RewardType.MANUAL:
            return True
        return self.reward_amount > 0


class RankRewardSettings(TimeStampedModel):
    """Singleton config for the rank-reward engine (dark-launched OFF).

    Reaching a reward-enabled rank only ever creates a *pending* reward event that
    leadership must approve and mark paid — the system never moves ISK. When rewards
    are first enabled the baseline is snapshotted (each pilot's current highest rank),
    so nothing is ever granted retroactively for a rank a pilot already held.
    """

    class Currency(models.TextChoices):
        ISK = "isk", "ISK"
        PLEX = "plex", "PLEX"
        MANUAL = "manual", _("Manual")

    class Strategy(models.TextChoices):
        CONSERVATIVE = "conservative", _("Conservative")
        STANDARD = "standard", _("Standard")
        AGGRESSIVE = "aggressive", _("Aggressive")

    rewards_enabled = models.BooleanField(
        default=False,
        help_text=_("Master switch. Off = ranks/titles work but no reward events are ever created."),
    )
    baseline_established_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_("When the future-only reward baseline was last (re)snapshotted."),
    )
    monthly_budget = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Fallback monthly ISK incentive budget when income data isn't used."),
    )
    max_income_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text=_("Max %% of recent monthly corp income to spend on rank rewards (0 = ignore income)."),
    )
    monthly_cap = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Hard ceiling on monthly reward liability (0 = no cap)."),
    )
    payout_currency = models.CharField(
        max_length=8, choices=Currency.choices, default=Currency.ISK
    )
    plex_isk_rate = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("ISK per PLEX override for liability estimates (0 = use live market price)."),
    )
    default_strategy = models.CharField(
        max_length=16, choices=Strategy.choices, default=Strategy.STANDARD
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    @classmethod
    def load(cls) -> RankRewardSettings:
        return cls.objects.first() or cls.objects.create()


class PilotRankBaseline(TimeStampedModel):
    """A pilot's highest qualified rank at the moment rewards were enabled.

    The future-only guarantee: a pilot only earns a reward for a reward-enabled
    rank strictly **above** ``baseline_min_kills``. Storing the threshold value
    (not just the FK) keeps the guarantee stable even if leaders later rename,
    reorder, delete or add ranks.
    """

    character_id = models.BigIntegerField(unique=True, db_index=True)
    character_name = models.CharField(max_length=128, blank=True)
    baseline_rank = models.ForeignKey(
        CombatRankTitle, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    baseline_min_kills = models.PositiveIntegerField(
        default=0, help_text=_("Threshold of the pilot's highest rank at baseline time.")
    )
    baseline_kills = models.PositiveIntegerField(default=0)
    established_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"baseline {self.character_id} @ {self.baseline_min_kills}"


class RankRewardEvent(TimeStampedModel):
    """A pending/approved/paid reward for a pilot reaching a reward-enabled rank.

    Created only for enrolled pilots (linked account + valid ESI token), only for
    ranks above their baseline, and only while rewards are enabled. The unique
    constraint makes generation idempotent — a pilot can never earn the same rung
    twice. Amounts/names are snapshotted so history survives ladder edits.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        PAID = "paid", _("Paid")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=128, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rank_reward_events",
    )
    rank = models.ForeignKey(
        CombatRankTitle, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reward_events",
    )
    rank_name = models.CharField(max_length=64)
    rank_min_kills = models.PositiveIntegerField(default=0)
    previous_rank_name = models.CharField(max_length=64, blank=True)
    kills_at_award = models.PositiveIntegerField(default=0)
    achieved_at = models.DateTimeField(default=timezone.now)

    reward_type = models.CharField(max_length=8, choices=RewardType.choices, default=RewardType.NONE)
    reward_amount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    reward_item_type_id = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_reference = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One reward per pilot per rung, ever — idempotent generation.
            models.UniqueConstraint(
                fields=["character_id", "rank_min_kills"], name="uniq_reward_char_rank"
            ),
        ]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="rre_status_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.character_name or self.character_id} → {self.rank_name} [{self.status}]"


class PilotRankNotification(TimeStampedModel):
    """The highest combat rung a pilot has already been *celebrated* for.

    Distinct from the reward baseline: this fires the pilot-facing "you made <rank>!"
    Pingboard note for **every** rung (not only reward-bearing ones), exactly once per
    rung. A pilot seen for the first time is baselined at their current rung **without**
    a notification, so enabling the feature never floods pilots with a backlog of ranks
    they earned before it existed (future-only). ``last_notified_min_kills`` is the
    rung's kill threshold — monotonic up the ladder — so a rank-up is simply a higher
    threshold than the one on file, and updating it makes re-notification impossible.
    """

    # ``unique=True`` already provides the index — no separate ``db_index`` (avoids the
    # redundant second btree the July perf audit removed elsewhere).
    character_id = models.BigIntegerField(unique=True)
    character_name = models.CharField(max_length=128, blank=True)
    last_notified_min_kills = models.PositiveIntegerField(default=0)
    last_notified_rank_name = models.CharField(max_length=64, blank=True)
    last_notified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.character_name or self.character_id} notified @ {self.last_notified_rank_name}"


class MonthlyPilotKillStat(models.Model):
    """Per-pilot, per-calendar-month PvP aggregate — the fast path for historical
    rankings (month and year) over a 147k+ killmail board.

    Built by a batched, idempotent backfill and refreshed incrementally for the
    current month. Home-corp attacker rows drive the kill columns; home-corp victim
    rows drive the loss columns, exactly matching the live leaderboards' PvP-only
    definitions, so a historical month reproduces the same boards.
    """

    character_id = models.BigIntegerField(db_index=True)
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()  # 1..12

    kills = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    solo_kills = models.IntegerField(default=0)
    final_blows = models.IntegerField(default=0)
    isk_destroyed = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    isk_lost = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    points = models.IntegerField(default=0)
    active_days = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character_id", "year", "month"], name="uniq_monthly_pilot_period"
            ),
        ]
        indexes = [
            # Period scans for a whole month/year, ordered by the ranked metric.
            models.Index(fields=["year", "month"], name="mpks_year_month_idx"),
            models.Index(fields=["year", "month", "-kills"], name="mpks_ym_kills_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.character_id} {self.year}-{self.month:02d}: {self.kills}k/{self.losses}l"


# --------------------------------------------------------------------------- #
#  KB-20 — real-time ingest fallback (R2Z2 killstream) + source health
# --------------------------------------------------------------------------- #
class KillstreamState(TimeStampedModel):
    """State for the OPTIONAL real-time killmail fallback (KB-20).

    zKillboard's R2Z2 sequence feed can be tapped for near-real-time home-corp kills, but
    it is a **supplementary fallback, never a replacement** for the authoritative feeds
    (the ESI Director corp feed, the zKillboard query-API poll, and the EVE Ref archives),
    which always run. This singleton holds the master switch (dark-launched **OFF**) and
    the resumable sequence cursor, plus light run stats. Because it is off by default and
    ingestion is idempotent, enabling/disabling it or letting it fall behind never loses a
    killmail — the primaries + EVE Ref remain the completeness backstop.
    """

    enabled = models.BooleanField(
        default=False,
        help_text=_(
            "Optional real-time fallback feed (zKillboard R2Z2). The primary sources "
            "(ESI Director feed, the zKill query poll and EVE Ref archives) always run; "
            "turn this on only for lower-latency ingest or as a supplementary source."
        ),
    )
    last_sequence = models.BigIntegerField(
        null=True, blank=True,
        help_text=_("The last R2Z2 sequence number consumed — the resumable cursor."),
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_run_scanned = models.IntegerField(default=0)
    last_run_ingested = models.IntegerField(default=0)
    ingested_total = models.BigIntegerField(default=0)

    @classmethod
    def load(cls) -> KillstreamState:
        return cls.objects.first() or cls.objects.create()

    def __str__(self) -> str:
        return f"killstream @ {self.last_sequence} ({'on' if self.enabled else 'off'})"


class IngestSourceHealth(models.Model):
    """Per-source killmail-ingest health for the source-precedence / freshness check (KB-20).

    One row per feed (``zkill_query``, ``esi_corp``, ``killstream``, …). Each discovery
    path records the outcome of its last run so leadership can see, at a glance, which
    feeds are healthy and how fresh the board is — and so a silent feed outage (like the
    historic "tasks existed but were never scheduled" bug) is visible instead of invisible.
    Purely observational: it never gates ingestion.
    """

    source = models.CharField(max_length=32, unique=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_count = models.IntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.IntegerField(default=0)

    def __str__(self) -> str:
        if self.last_success_at:
            return f"{self.source}: {self.last_count} @ {self.last_success_at:%Y-%m-%d %H:%M}"
        return self.source

    @classmethod
    def record(cls, source: str, *, count: int | None = None, error: str | None = None) -> None:
        """Record a run outcome for ``source`` (idempotent upsert).

        A success (``error`` falsy) stamps ``last_success_at``, sets ``last_count`` and
        clears the failure streak; a failure stamps ``last_error``/``last_error_at`` and
        increments ``consecutive_failures``. Never raises into the caller — health
        recording must not break an ingest task.
        """
        now = timezone.now()
        try:
            row, _created = cls.objects.get_or_create(source=source)
            row.last_run_at = now
            if error:
                row.last_error = str(error)[:300]
                row.last_error_at = now
                row.consecutive_failures += 1
            else:
                row.last_success_at = now
                row.last_count = int(count or 0)
                row.last_error = ""
                row.consecutive_failures = 0
            row.save()
        except Exception:  # noqa: BLE001 — observability must never break ingestion
            logging.getLogger("forca.killboard").debug(
                "ingest health record failed for %s", source, exc_info=True
            )


# --------------------------------------------------------------------------- #
#  KB-28 — per-user API token (the killboard REST API)
# --------------------------------------------------------------------------- #
class KillboardApiToken(models.Model):
    """A member's personal bearer token for the killboard REST API (KB-28).

    Only the SHA-256 **hash** of the token is stored — the plaintext is shown once, at
    creation, and can never be recovered (a leaked DB dump therefore yields no usable
    tokens). A token authenticates as its owning user and carries exactly the account's
    RBAC standing: it is a credential *for a member*, never a way to exceed one. Revoking
    is a soft delete (``revoked_at``) so the audit trail — who held a token, when it was
    last used — survives the revoke.

    The 8-char ``prefix`` (the token's own leading characters, not a secret) lets a member
    tell their tokens apart in the management UI without ever re-displaying the secret.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="killboard_api_tokens"
    )
    name = models.CharField(
        max_length=100, blank=True,
        help_text=_("A label to tell this token apart from your others (e.g. “Discord bot”)."),
    )
    # SHA-256 hex digest of the plaintext token — 64 chars, unique, indexed for the
    # per-request auth lookup. Never the token itself.
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=12, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The management page lists a user's live tokens newest-first.
            models.Index(fields=["user", "-created_at"], name="kbtok_user_created_idx"),
        ]

    def __str__(self) -> str:
        state = "revoked" if self.revoked_at else "active"
        return f"api token {self.prefix}… ({state})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @staticmethod
    def hash_key(raw: str) -> str:
        """SHA-256 hex of a plaintext token — the only form we ever persist or compare."""
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, user, name: str = "") -> tuple[KillboardApiToken, str]:
        """Mint a token for ``user``; returns ``(row, plaintext)``.

        The plaintext is returned ONCE for the caller to show the member and is never
        stored — only its hash is. Uses a 256-bit url-safe secret from ``secrets``.
        """
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user, name=(name or "")[:100], key_hash=cls.hash_key(raw), prefix=raw[:8]
        )
        return token, raw

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])


# --------------------------------------------------------------------------- #
#  KB-29 — realtime push feed OUT (the outbound SSE / poll event ring buffer)
# --------------------------------------------------------------------------- #
class KillboardStreamEvent(models.Model):
    """One row per newly-ingested home-corp killmail, appended for the outbound stream (KB-29).

    A small **ring buffer**: the monotonic ``seq`` primary key is the resume cursor a client
    presents (``Last-Event-ID`` / ``?after_seq=``), and a nightly-cadence prune keeps only the
    most recent ``KILLBOARD_STREAM_RETENTION`` rows (the live feed is not history — the board,
    the API and EVE Ref are). Rows are written by :func:`apps.killboard.stream.emit_stream_event`
    at the single post-ingest seam, and **only for kills within ~48h of now** so a years-old
    EVE Ref backfill can never flood the feed.

    Every topic-filter dimension (kind, sec band, ISK band via ``total_value``, ship class,
    system, pilot, plus the member-gated ``needs_srp`` / ``deviated`` flags) is denormalised
    onto the row, so serving the stream is a single indexed ``seq >`` scan with no ``Killmail``
    join and no per-event SRP/deviation recomputation. Nothing here is sensitive: it is ids,
    public flags and two precomputed booleans — never a payout figure, pilot name or fit name
    (those stay gated on the board/API). The ``needs_srp`` / ``deviated`` booleans are still
    withheld from the public-read (anonymous) payload; they only reach members.
    """

    seq = models.BigAutoField(primary_key=True)
    killmail = models.ForeignKey(
        Killmail, on_delete=models.CASCADE, related_name="stream_events", db_index=False
    )
    # --- payload (denormalised so the stream never joins Killmail) ----------
    killmail_hash = models.CharField(max_length=64)
    kill_time = models.DateTimeField()
    home_role = models.CharField(max_length=10, choices=Killmail.HomeRole.choices)
    sec_band = models.CharField(max_length=10, choices=SecBand.choices)
    system_id = models.IntegerField()
    ship_class = models.CharField(max_length=32, blank=True)
    victim_ship_type_id = models.IntegerField()
    victim_character_id = models.BigIntegerField(null=True, blank=True)
    victim_corporation_id = models.BigIntegerField(null=True, blank=True)
    total_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    is_solo = models.BooleanField(default=False)
    is_npc = models.BooleanField(default=False)
    is_awox = models.BooleanField(default=False)
    # --- member-gated topic flags (precomputed at emission) -----------------
    needs_srp = models.BooleanField(default=False)
    deviated = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        # The stream is served newest-after-cursor; the PK (seq) already provides the
        # ascending index for `seq > cursor`. `created` carries no separate index — the prune
        # deletes by `seq <` a threshold, which the PK serves.
        ordering = ["seq"]

    def __str__(self) -> str:
        return f"stream event {self.seq} (killmail {self.killmail_id})"


# --------------------------------------------------------------------------- #
#  KB-30 — per-pilot subscriptions (personal, RBAC-aware notification feeds)
# --------------------------------------------------------------------------- #
class SubscriptionEventType(models.TextChoices):
    """What a subscription fires on. The killmail-driven types (see
    ``KillboardSubscription.KILLMAIL_EVENT_TYPES``) are matched by the cursor-consumer
    task against the KB-29 stream ring buffer; ``rank_up`` / ``watchlist_hit`` are pushed
    from the existing rank-notify / watchlist-tripwire emission points (never re-derived)."""

    MY_KILL = "my_kill", _("One of my pilots got on a kill")
    MY_LOSS = "my_loss", _("One of my pilots died")
    MY_LOSS_SRP_PENDING = "my_loss_srp_pending", _("My loss is eligible for SRP")
    WATCHLIST_HIT = "watchlist_hit", _("A watched entity appeared on a killmail")
    RANK_UP = "rank_up", _("I reached a new combat rank")
    FILTER_MATCH = "filter_match", _("A kill matched my saved filter")


class SubscriptionChannel(models.TextChoices):
    """Where a matched event is delivered.

    ``notify`` uses Pingboard's per-user route (the in-app inbox plus any verified
    Slack/Telegram/WhatsApp DM handles the pilot has linked — and Discord DMs once
    Pingboard's bot-mode DM provider ships; the Discord provider is webhook-only today).
    ``email`` uses Django's ``EMAIL_BACKEND``. ``webhook`` is an HTTPS POST (SSRF-guarded).
    ``rss`` is pull-only: nothing is pushed — the tokenised feed URL renders the matched
    events at read time.
    """

    NOTIFY = "notify", _("In-app + linked chat DMs")
    EMAIL = "email", _("Email")
    WEBHOOK = "webhook", _("Webhook (HTTPS POST)")
    RSS = "rss", _("RSS / Atom feed")


class KillboardSubscription(TimeStampedModel):
    """A member's personal, self-serve killboard notification (KB-30).

    RBAC-aware by construction: a subscription only ever delivers data the owning member
    could already see on the board at member tier — the ``my_*`` types resolve against the
    user's own linked pilots (Linked Pilots), and every payload is the public KB-29 stream
    shape (ids, public flags, and the member-visible ``needs_srp`` / ``deviated`` booleans),
    never a payout figure, deviation detail or fit name. There is no way to subscribe another
    pilot into your feed, and no officer-only datum reaches any channel.

    The ``rss_token`` is a **separate lightweight read-only secret**, independent of the
    KB-28 API bearer token: it authorises reading exactly one feed and is not an account
    credential. Regenerating or deleting the subscription revokes it.
    """

    # Event types matched by the cursor-consumer against the KB-29 stream ring buffer.
    # rank_up / watchlist_hit are pushed from their own existing emission points instead.
    KILLMAIL_EVENT_TYPES = frozenset({
        SubscriptionEventType.MY_KILL,
        SubscriptionEventType.MY_LOSS,
        SubscriptionEventType.MY_LOSS_SRP_PENDING,
        SubscriptionEventType.FILTER_MATCH,
    })

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="killboard_subscriptions"
    )
    event_type = models.CharField(max_length=24, choices=SubscriptionEventType.choices)
    channel = models.CharField(max_length=12, choices=SubscriptionChannel.choices)
    # filter_match: a killfeed_rules-style clause dict; watchlist_hit: optional {"watchlist_id": n}.
    params = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    webhook_url = models.URLField(max_length=500, blank=True)
    # A read-only feed secret (urlsafe), only set for the rss channel. Nullable+unique so many
    # non-rss rows (NULL) coexist while every real token stays globally unique.
    rss_token = models.CharField(max_length=64, null=True, blank=True, unique=True)
    # Resume cursor into KillboardStreamEvent.seq — advanced every dispatch run so a
    # subscription never re-fires a processed event, and initialised to the current tip at
    # creation so it never back-fires the buffered history.
    last_seq = models.BigIntegerField(default=0)
    last_fired = models.DateTimeField(null=True, blank=True)
    # Webhook dead-letter: consecutive delivery failures; the subscription auto-disables once
    # it crosses the configured ceiling, with a user-visible reason.
    consecutive_failures = models.IntegerField(default=0)
    disabled_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The dispatch task scans enabled killmail-event subscriptions by type.
            models.Index(fields=["event_type", "enabled"], name="kbsub_type_enabled_idx"),
            # The management page lists a user's subscriptions newest-first.
            models.Index(fields=["user", "-created_at"], name="kbsub_user_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} → {self.channel} (user {self.user_id})"

    @property
    def is_killmail_event(self) -> bool:
        return self.event_type in self.KILLMAIL_EVENT_TYPES

    def regenerate_rss_token(self) -> str:
        """Mint a fresh feed secret (revoking the old URL). Returns the new token."""
        self.rss_token = secrets.token_urlsafe(32)
        return self.rss_token


class KillboardSubscriptionEvent(models.Model):
    """One matched event recorded against a subscription (KB-30).

    Backs the RSS feed (rendered at read time) and is the delivery audit trail for the push
    channels. Holds only the member-visible public payload — never a sensitive field. Pruned
    to the newest ``KILLBOARD_SUBSCRIPTION_FEED_KEEP`` rows per subscription so the feed and
    the table stay bounded.
    """

    subscription = models.ForeignKey(
        KillboardSubscription, on_delete=models.CASCADE, related_name="feed_events"
    )
    event_type = models.CharField(max_length=24, choices=SubscriptionEventType.choices)
    killmail_id = models.BigIntegerField(null=True, blank=True)
    seq = models.BigIntegerField(null=True, blank=True)
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=500, blank=True)
    link = models.CharField(max_length=300, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["subscription", "-created"], name="kbsubev_sub_created_idx"),
        ]

    def __str__(self) -> str:
        return f"feed event {self.pk} ({self.event_type} → sub {self.subscription_id})"
