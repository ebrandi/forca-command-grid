"""KB-24 — require/exclude rule engine for the kill-feed.

Extends the two ISK thresholds with aa-killtracker-style clauses over data we already store:
attacker count, security band, victim ship class, jumps-from-staging, NPC/awox/solo, and a
doctrine-deviated-losses clause. Everything is AND-combined with the ISK floor, and every
clause is off by default, so an unconfigured feed behaves exactly as before.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.doctrines.hulls import CLASS_ORDER, hull_meta

from .models import Killmail, SecBand

# For the officer form. Values match Killmail.sec_band; labels are translatable.
SEC_BANDS: list[tuple[str, object]] = [
    (SecBand.HIGHSEC, _("High-sec")),
    (SecBand.LOWSEC, _("Low-sec")),
    (SecBand.NULLSEC, _("Null-sec")),
    (SecBand.WORMHOLE, _("Wormhole")),
    (SecBand.ABYSSAL, _("Abyssal")),
    (SecBand.POCHVEN, _("Pochven")),
    (SecBand.UNKNOWN, _("Unknown")),
]

SHIP_CLASSES = CLASS_ORDER  # Frigate … Capital, Other


def ship_classes_for(type_ids) -> dict[int, str]:
    """``{victim_ship_type_id: broad class}`` for a batch of hulls (one SDE query)."""
    return {tid: meta["hull_class"] for tid, meta in hull_meta(set(type_ids)).items()}


def staging_distances(cfg) -> dict[int, int] | None:
    """Gate-hop distances from the active staging system, or ``None`` when the
    jumps-from-staging clause is off or no staging system is configured/available."""
    if not cfg.max_jumps_from_staging:
        return None
    try:
        from apps.navigation.highsec_exit import gate_distances
        from apps.readiness.models import StagingSystem
    except ImportError:  # navigation/readiness not installed in this deployment
        return None
    staging = StagingSystem.objects.filter(active=True).first()
    if staging is None:
        return None
    return gate_distances(staging.system_id)


def evaluate(km, cfg, *, attacker_count: int, ship_class: str,
             staging_distance: dict[int, int] | None) -> bool:
    """True iff the killmail clears the ISK floor AND every configured clause.

    With all rule fields at their defaults this reduces to the original threshold check.
    """
    is_loss = km.home_corp_role == Killmail.HomeRole.VICTIM

    # ISK floor (original behaviour: 0 mutes that direction).
    threshold = cfg.min_loss_value if is_loss else cfg.min_kill_value
    if threshold <= 0 or km.total_value < threshold:
        return False

    if cfg.exclude_npc and km.is_npc:
        return False
    if cfg.exclude_awox and km.is_awox:
        return False
    if cfg.require_solo and not km.is_solo:
        return False

    if cfg.min_attackers and attacker_count < cfg.min_attackers:
        return False
    if cfg.max_attackers and attacker_count > cfg.max_attackers:
        return False

    if cfg.sec_bands and km.sec_band not in cfg.sec_bands:
        return False

    if cfg.ship_classes and ship_class not in cfg.ship_classes:
        return False

    if cfg.max_jumps_from_staging:
        # Clause is set but staging is unavailable, or the kill's system is out of range.
        if staging_distance is None:
            return False
        hops = staging_distance.get(km.solar_system_id)
        if hops is None or hops > cfg.max_jumps_from_staging:
            return False

    if cfg.losses_deviated_only:
        if not is_loss:
            return False
        deviation = getattr(km, "fit_deviation", None)
        if deviation is None or deviation.is_clean:
            return False

    return True
