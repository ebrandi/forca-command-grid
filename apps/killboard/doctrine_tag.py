"""Per-killmail doctrine tagging (KB-13).

Match a home-corp loss's hull to the canonical doctrine fit and store the tag on
``Killmail.doctrine_fit`` — the foundation the fit-deviation coaching layer
(KB-14) builds on. Only the corp's own losses are tagged; a tag on a kill (the
victim is an enemy) would be meaningless.
"""
from __future__ import annotations

from apps.doctrines.services import best_doctrine_fit

from .models import FitDeviation, Killmail

# EVE inventory flagIDs for fitted slots (low/med/high modules, rigs, subsystems,
# drones). Cargo (5) and other holds are excluded so the diff is fit-vs-fit.
_FITTING_FLAGS = frozenset(
    {*range(11, 19), *range(19, 27), *range(27, 35), *range(92, 100), *range(125, 133), 87}
)


def fitted_module_multiset(killmail: Killmail) -> dict[int, int]:
    """The ``{type_id: quantity}`` multiset of a killmail's FITTED-slot modules (cargo
    excluded), for module-aware doctrine matching (4.2)."""
    fitted: dict[int, int] = {}
    for item in killmail.items.all():
        if item.flag in _FITTING_FLAGS:
            qty = (item.quantity_destroyed or 0) + (item.quantity_dropped or 0)
            fitted[item.item_type_id] = fitted.get(item.item_type_id, 0) + qty
    return fitted


def tag_doctrine_fit(killmail: Killmail) -> None:
    """Set ``killmail.doctrine_fit`` to the best-matching active doctrine fit, or leave
    it null. Matching is **module-aware** (4.2): for a hull with several doctrine fits it
    tags the variant whose modules best match what was actually fitted, not just the
    first by priority — so the deviation diff and SRP valuation reflect the fit flown.
    Re-runnable: called at ingest after valuation and from the ``retag_doctrine_fits``
    backfill, writing only when the tag actually changes."""
    if killmail.home_corp_role != Killmail.HomeRole.VICTIM:
        return
    fit = best_doctrine_fit(killmail.victim_ship_type_id, fitted_module_multiset(killmail))
    new_id = fit.id if fit else None
    if killmail.doctrine_fit_id != new_id:
        killmail.doctrine_fit = fit
        killmail.save(update_fields=["doctrine_fit"])


def _multiset_from_modules(modules) -> dict[int, int]:
    """A {type_id: quantity} multiset from a DoctrineFit.modules list."""
    counts: dict[int, int] = {}
    for module in modules or []:
        tid = module.get("type_id")
        if tid:
            counts[int(tid)] = counts.get(int(tid), 0) + int(module.get("quantity", 1) or 1)
    return counts


def compute_fit_deviation(killmail: Killmail) -> None:
    """Diff a doctrine-tagged loss's fitted modules against its canonical fit and
    store a FitDeviation (KB-14 / SRP-7): ``missing`` = in the fit but not on the
    loss, ``extra`` = on the loss but not in the fit, each ``[{type_id, quantity}]``.
    No tag → no deviation (any stale row is removed). Re-runnable, like tagging.

    A DoctrineFit's modules list is cargo-inclusive (the importers aggregate
    across slots AND cargo), so the two sides are split asymmetrically on purpose:
    ``missing`` is measured against EVERYTHING the pilot had aboard (fitted +
    cargo) — a doctrine consumable carried as a cargo spare must not read as
    missing — while ``extra`` is measured against the FITTED-slot items only, so
    unrelated cargo loot isn't flagged as off-doctrine. Ammo quantity drift still
    adds a little noise, which the aggregate board tolerates.
    """
    fit = killmail.doctrine_fit
    if fit is None:
        FitDeviation.objects.filter(killmail=killmail).delete()
        return

    canonical = _multiset_from_modules(fit.modules)
    all_items: dict[int, int] = {}
    fitted: dict[int, int] = {}
    for item in killmail.items.all():
        qty = (item.quantity_destroyed or 0) + (item.quantity_dropped or 0)
        all_items[item.item_type_id] = all_items.get(item.item_type_id, 0) + qty
        if item.flag in _FITTING_FLAGS:
            fitted[item.item_type_id] = fitted.get(item.item_type_id, 0) + qty

    missing = [
        {"type_id": tid, "quantity": canonical[tid] - all_items.get(tid, 0)}
        for tid in canonical
        if canonical[tid] - all_items.get(tid, 0) > 0
    ]
    extra = [
        {"type_id": tid, "quantity": fitted[tid] - canonical.get(tid, 0)}
        for tid in fitted
        if fitted[tid] - canonical.get(tid, 0) > 0
    ]
    FitDeviation.objects.update_or_create(
        killmail=killmail, defaults={"doctrine_fit": fit, "missing": missing, "extra": extra}
    )
