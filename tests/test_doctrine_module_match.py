"""4.2 — Module-aware doctrine fit matching (deviation-grade, not hull-only).

Acceptance: for a hull with several doctrine fits, matching picks the variant whose
modules best match what was ACTUALLY fitted (not just the first by priority), so the
killboard deviation tag and SRP DOCTRINE_FIT valuation reflect the fit flown. A
single-fit hull, an empty fit, or no fitted data is byte-for-byte the old hull-only
result.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.services import best_doctrine_fit, match_doctrine_fit
from apps.killboard.doctrine_tag import fitted_module_multiset, tag_doctrine_fit
from apps.killboard.models import Killmail, KillmailItem

pytestmark = pytest.mark.django_db

FEROX = 16227
GUN_A = 2977   # blaster (shield-DPS variant)
GUN_B = 2969   # railgun (long-range variant)
CARGO_JUNK = 34   # tritanium in the hold — must never count as a fitted module
LOW_SLOT_FLAG = 27  # in _FITTING_FLAGS
CARGO_FLAG = 5      # excluded


def _two_ferox_fits():
    cat = DoctrineCategory.objects.create(key="bc", label="Battlecruisers")
    d1 = Doctrine.objects.create(name="Ferox Blaster", category=cat, priority=90)
    fit_a = DoctrineFit.objects.create(
        doctrine=d1, name="Ferox (blaster)", ship_type_id=FEROX,
        modules=[{"type_id": GUN_A, "quantity": 7}],
    )
    d2 = Doctrine.objects.create(name="Ferox Rail", category=cat, priority=80)
    fit_b = DoctrineFit.objects.create(
        doctrine=d2, name="Ferox (rail)", ship_type_id=FEROX,
        modules=[{"type_id": GUN_B, "quantity": 7}],
    )
    return fit_a, fit_b


def _ferox_loss(km_id=910001, *, fitted=None, cargo=None):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_time=timezone.now(), solar_system_id=30000142,
        victim_character_id=6001, victim_ship_type_id=FEROX,
        total_value=Decimal("50000000"), involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
    )
    idx = 0
    for tid, qty in (fitted or {}).items():
        KillmailItem.objects.create(killmail=km, idx=idx, item_type_id=tid,
                                    flag=LOW_SLOT_FLAG, quantity_destroyed=qty)
        idx += 1
    for tid, qty in (cargo or {}).items():
        KillmailItem.objects.create(killmail=km, idx=idx, item_type_id=tid,
                                    flag=CARGO_FLAG, quantity_destroyed=qty)
        idx += 1
    return km


def test_module_match_overrides_priority():
    fit_a, fit_b = _two_ferox_fits()
    # Fitted with the rail (fit_b's module), even though fit_a's doctrine is higher priority.
    chosen = best_doctrine_fit(FEROX, {GUN_B: 7})
    assert chosen.id == fit_b.id
    # And the blaster fit is chosen when the blaster is fitted.
    assert best_doctrine_fit(FEROX, {GUN_A: 7}).id == fit_a.id


def test_empty_fitted_falls_back_to_priority():
    fit_a, _fit_b = _two_ferox_fits()
    assert best_doctrine_fit(FEROX, {}).id == fit_a.id        # highest priority
    assert best_doctrine_fit(FEROX, None).id == fit_a.id


def test_single_fit_hull_matches_hull_only():
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Solo", category=cat, priority=50)
    fit = DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=587,
                                     modules=[{"type_id": 484, "quantity": 2}])
    # With one candidate, module-aware == hull-only, regardless of fitted data.
    assert best_doctrine_fit(587, {999: 1}).id == fit.id
    assert best_doctrine_fit(587, None).id == match_doctrine_fit(587).id


def test_no_candidate_returns_none():
    _two_ferox_fits()
    assert best_doctrine_fit(99999, {GUN_A: 1}) is None


def test_fitted_multiset_excludes_cargo():
    km = _ferox_loss(fitted={GUN_B: 7}, cargo={CARGO_JUNK: 5000})
    ms = fitted_module_multiset(km)
    assert ms == {GUN_B: 7}  # cargo tritanium not counted


def test_tag_doctrine_fit_picks_module_variant():
    _fit_a, fit_b = _two_ferox_fits()
    km = _ferox_loss(fitted={GUN_B: 7})
    tag_doctrine_fit(km)
    km.refresh_from_db()
    assert km.doctrine_fit_id == fit_b.id  # tagged to the rail fit actually flown


def test_srp_matched_doctrine_is_module_aware():
    from apps.srp import services as srp
    _fit_a, fit_b = _two_ferox_fits()
    km = _ferox_loss(fitted={GUN_B: 7})
    doctrine, fit = srp.matched_doctrine(km)
    assert fit.id == fit_b.id and doctrine.name == "Ferox Rail"


def _two_ferox_fits_with_cargo(*, with_slots: bool):
    """Two fits whose canonical modules carry heavy CARGO ammo — the case that broke
    the old summed-quantity metric (HIGH-1): the rail fit lists 5000 spare Spike, so a
    quantity-summing score would wrongly prefer the blaster fit for a rail loss."""
    cat = DoctrineCategory.objects.create(key="bc", label="BC")
    d1 = Doctrine.objects.create(name="Ferox Rail", category=cat, priority=90)
    rail_mods = [{"type_id": GUN_B, "quantity": 7}, {"type_id": 12608, "quantity": 5000}]
    blaster_mods = [{"type_id": GUN_A, "quantity": 7}, {"type_id": 12608, "quantity": 200}]
    if with_slots:
        rail_mods[0]["slot"] = "high"
        rail_mods[1]["slot"] = "cargo"
        blaster_mods[0]["slot"] = "high"
        blaster_mods[1]["slot"] = "cargo"
    rail = DoctrineFit.objects.create(doctrine=d1, name="rail", ship_type_id=FEROX, modules=rail_mods)
    d2 = Doctrine.objects.create(name="Ferox Blaster", category=cat, priority=80)
    blaster = DoctrineFit.objects.create(doctrine=d2, name="blaster", ship_type_id=FEROX,
                                         modules=blaster_mods)
    return rail, blaster


@pytest.mark.parametrize("with_slots", [True, False])
def test_cargo_ammo_mass_does_not_bias_match(with_slots):
    # Pilot flew the RAIL fit; heavy canonical cargo must NOT flip the match to blaster.
    rail, _blaster = _two_ferox_fits_with_cargo(with_slots=with_slots)
    chosen = best_doctrine_fit(FEROX, {GUN_B: 7})  # only the rail gun actually fitted
    assert chosen.id == rail.id


def test_retag_command_is_module_aware():
    from django.core.management import call_command
    fit_a, fit_b = _two_ferox_fits()  # fit_a=blaster (priority 90), fit_b=rail (priority 80)
    km = _ferox_loss(fitted={GUN_B: 7})  # rail actually fitted
    # Pre-seed the WRONG hull-only-first-priority tag to prove the backfill corrects it.
    km.doctrine_fit = fit_a
    km.save(update_fields=["doctrine_fit"])
    call_command("retag_doctrine_fits")
    km.refresh_from_db()
    assert km.doctrine_fit_id == fit_b.id  # module-aware, not hull-only first-priority
