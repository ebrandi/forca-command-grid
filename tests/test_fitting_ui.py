"""WS-13 UI/API batch — regression tests.

Covers the API bug-fixes (killmail implant slot, save dedup, EFT export cargo quantities,
stock-coverage charges, EFT-import unresolved warning, number formatting) and the new editor /
telemetry panels (racks, target profile, applied DPS, mode selector, projected effects, fleet
boosts, mining, EWAR, fighters, mutated attributes, ship mode / lock / warp rows).

The live editor panels render client-side (Alpine), so template presence is asserted two ways:
the server-rendered scaffolding (loops, labels, options) via the Django test client, and the
telemetry sections via ``render_to_string`` with a real engine telemetry dict plus the synthetic
sub-sections the seeded Rifter fixture cannot produce (mining / fighters / jam / mode).
"""
from __future__ import annotations

import json

import pytest
from django.template.loader import render_to_string
from django.urls import reverse

from apps.fitting import services
from apps.fitting.engine.types import SkillProfile
from apps.fitting.models import Fit
from apps.fitting.templatetags.fitting_fmt import num, rangem

from ._fitting_utils import AC, DC, FUSION, RIFTER, make_member, seed_dogma


# --------------------------------------------------------------------------- #
# Fixtures + extra seeding
# --------------------------------------------------------------------------- #
@pytest.fixture
def dogma(db):
    seed_dogma()
    return True


@pytest.fixture
def owner(dogma):
    return make_member("eve:9100", 9100, "WS13 Owner")


def _seed_extra_types():
    """Seed the special SDE types WS-13 panels need: an implant, a subsystem, a fighter, a
    fleet-boost charge, a tactical mode and a projectable EWAR module."""
    from apps.fitting.engine import attributes as A
    from apps.sde.models import (
        SdeCategory,
        SdeDogmaEffect,
        SdeGroup,
        SdeType,
        SdeTypeEffect,
    )
    for cid, name in [(20, "Implant"), (32, "Subsystem"), (87, "Fighter")]:
        SdeCategory.objects.get_or_create(category_id=cid, defaults={"name": name})
    groups = [
        (701, 20, "Cyberimplant"), (702, 32, "Offensive Subsystem"),
        (703, 87, "Light Fighter"), (704, 8, "Shield Command Burst Charges"),
        (705, 6, "Ship Modifiers"), (706, 7, "ECM"),
    ]
    for gid, cid, name in groups:
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": cid, "name": name})
    types = [
        (81000, 701, "Ocular Filter - Basic"),          # implant
        (81001, 702, "Test Offensive Subsystem"),        # subsystem
        (81002, 703, "Test Light Fighter"),              # fighter
        (81003, 704, "Shield Harmonizing Charge"),       # fleet-boost charge
        (81004, 705, "Rifter Defense Mode"),             # tactical mode (matches Rifter hull)
        (81005, 706, "Test ECM Multispectral Jammer"),   # projectable ewar module
    ]
    for tid, gid, name in types:
        SdeType.objects.get_or_create(type_id=tid, defaults={"group_id": gid, "name": name})
    # An offensive effect + a module carrying it → search_projected must offer it.
    SdeDogmaEffect.objects.get_or_create(
        effect_id=9901, defaults={"name": "ecmTest", "is_offensive": True})
    SdeTypeEffect.objects.get_or_create(type_id=81005, effect_id=9901, defaults={"is_default": True})
    # A burst charge is defined by carrying warfareBuff1ID (2468) — command_burst_charges
    # filters on it so the charges' blueprint types never appear in the panel.
    from apps.sde.models import SdeTypeAttribute
    SdeTypeAttribute.objects.get_or_create(type_id=81003, attribute_id=2468,
                                           defaults={"value": 10.0})
    SdeType.objects.get_or_create(type_id=81006, defaults={
        "group_id": 704, "name": "Shield Harmonizing Charge Blueprint"})
    return True


# --------------------------------------------------------------------------- #
# API-18: number formatting filters
# --------------------------------------------------------------------------- #
def test_num_filter_trims_and_groups():
    assert num(1234.0) == "1,234"        # trailing .0 dropped
    assert num(1234.5) == "1,234.5"      # real decimal kept
    assert num(11150.0) == "11,150"      # thousands separator
    assert num(0) == "0"
    assert num(None) == "—"              # non-numeric → em dash
    assert num(0.45, 2) == "0.45"


def test_rangem_filter_km_and_metres():
    assert rangem(40000) == "40 km"      # ≥10 km → km, trailing .0 trimmed
    assert rangem(42500) == "42.5 km"
    assert rangem(8000) == "8,000 m"     # <10 km → grouped metres
    assert rangem(0) == "0 m"
    assert rangem(None) == "—"


# --------------------------------------------------------------------------- #
# Killmail import: implant lands in the implant rack (not silently low-slot)
# --------------------------------------------------------------------------- #
def test_killmail_implant_keeps_implant_slot(dogma):
    esi = {"ship_type_id": RIFTER, "items": [
        {"flag": 89, "type_id": 81000, "quantity": 1},   # ESI flag 89 = implant slot 1
        {"flag": 27, "type_id": AC, "quantity": 1},       # a high-slot module
    ]}
    ship, items = services.items_from_esi_fitting(esi)
    assert ship == RIFTER
    implant = next(i for i in items if i["type_id"] == 81000)
    assert implant["slot"] == "implant"        # regression: was silently "low"
    assert implant["state"] == "active"


# --------------------------------------------------------------------------- #
# API-16: EFT export cargo quantities round-trip
# --------------------------------------------------------------------------- #
def test_export_eft_cargo_quantity_round_trips(dogma):
    items = [{"type_id": FUSION, "slot": "cargo", "state": "offline",
              "charge_type_id": None, "quantity": 5}]
    text = services.export_eft(RIFTER, items, "Cargo Test")
    assert "Fusion S x5" in text
    reparsed = services.import_eft(text)
    cargo = next(i for i in reparsed["items"] if i["type_id"] == FUSION)
    assert cargo["slot"] == "cargo" and cargo["quantity"] == 5


# --------------------------------------------------------------------------- #
# API-12/13: stock coverage counts charges (ammo) as components
# --------------------------------------------------------------------------- #
def test_stock_coverage_includes_charges(dogma):
    # No stockpiles seeded → everything is "missing"; the charge must appear (previously it did
    # not, so a fit with no corp ammo read "all components available").
    items = [{"type_id": AC, "slot": "high", "state": "active",
              "charge_type_id": FUSION, "quantity": 1}]
    cov = services.stock_coverage(RIFTER, items)
    missing_ids = {m["type_id"] for m in cov["missing"]}
    assert FUSION in missing_ids
    assert AC in missing_ids


# --------------------------------------------------------------------------- #
# API-10: save dedup
# --------------------------------------------------------------------------- #
def test_save_revision_dedup_no_op(owner, dogma):
    parsed = services.import_eft(
        "[Rifter, Dedup]\n200mm AutoCannon I, Fusion S\nDamage Control I")
    fit = services.create_fit(owner, name="Dedup", ship_type_id=RIFTER, items=parsed["items"])
    assert fit.revisions.count() == 1
    # Identical payload → the same revision, no new row.
    same = services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"],
                                  user=owner, dedup=True)
    fit.refresh_from_db()
    assert same.pk == fit.current_revision.pk
    assert fit.revisions.count() == 1
    # A real change → a new revision.
    services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1],
                           user=owner, dedup=True)
    fit.refresh_from_db()
    assert fit.revisions.count() == 2


def test_save_view_reports_no_changes(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft("[Rifter, X]\nDamage Control I")
    fit = services.create_fit(owner, name="X", ship_type_id=RIFTER, items=parsed["items"])
    body = {"ship_type_id": RIFTER, "items": json.dumps(parsed["items"])}
    r1 = client.post(reverse("fitting:save", args=[fit.pk]), body,
                     HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r1.status_code == 200 and r1.json()["changed"] is False
    fit.refresh_from_db()
    assert fit.revisions.count() == 1
    # A change is reported as changed=True and creates a revision.
    r2 = client.post(reverse("fitting:save", args=[fit.pk]),
                     {"ship_type_id": RIFTER, "items": "[]"},
                     HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r2.json()["changed"] is True
    fit.refresh_from_db()
    assert fit.revisions.count() == 2


# --------------------------------------------------------------------------- #
# API-5: EFT-import unresolved warning banner
# --------------------------------------------------------------------------- #
def test_import_eft_surfaces_unresolved_banner(client, owner, dogma):
    client.force_login(owner)
    eft = "[Rifter, Bad]\nNonexistent Module 9000\nDamage Control I"
    resp = client.post(reverse("fitting:import_eft"), {"eft": eft}, follow=True)
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "could not be resolved" in body
    assert "Nonexistent Module 9000" in body
    # One-shot: a second load of the same page no longer shows the banner.
    fit = Fit.objects.filter(owner=owner).latest("created_at")
    again = client.get(reverse("fitting:detail", args=[fit.pk]))
    assert "Nonexistent Module 9000" not in again.content.decode()


# --------------------------------------------------------------------------- #
# API-4: subsystem / implant / booster / cargo racks are rendered
# --------------------------------------------------------------------------- #
def test_editor_renders_extra_racks(client, owner, dogma):
    _seed_extra_types()
    items = [
        {"type_id": 81001, "slot": "subsystem", "state": "active", "charge_type_id": None, "quantity": 1},
        {"type_id": 81000, "slot": "implant", "state": "active", "charge_type_id": None, "quantity": 1},
        {"type_id": FUSION, "slot": "cargo", "state": "offline", "charge_type_id": None, "quantity": 3},
    ]
    fit = services.create_fit(owner, name="Extras", ship_type_id=RIFTER, items=items)
    client.force_login(owner)
    body = client.get(reverse("fitting:detail", args=[fit.pk])).content.decode()
    # The extra-rack loop scaffolding is present…
    assert "['subsystem','implant','booster','cargo']" in body
    assert "extraLabel" in body
    # …and the imported items are seeded into the editor (so Alpine renders them in the racks).
    lab = body.split('id="lab-items"', 1)[1]
    assert "81001" in lab and "81000" in lab


# --------------------------------------------------------------------------- #
# API-8: full target-profile inputs + damage picker
# --------------------------------------------------------------------------- #
def test_editor_renders_target_profile_inputs(client, owner, dogma):
    fit = services.create_fit(owner, name="T", ship_type_id=RIFTER, items=[])
    client.force_login(owner)
    body = client.get(reverse("fitting:detail", args=[fit.pk])).content.decode()
    for anchor in ('id="tgt-dist"', 'id="tgt-ss"', 'id="tgt-sensor"', 'id="tgt-hp"',
                   'id="warp-au"', 'id="dmg-preset"'):
        assert anchor in body, anchor


def test_target_profile_payload_round_trips(client, owner, dogma):
    """The extended target keys the editor now sends all reach the engine (200, telemetry)."""
    client.force_login(owner)
    parsed = services.import_eft(
        "[Rifter, A]\n200mm AutoCannon I, Fusion S\n200mm AutoCannon I, Fusion S")
    resp = client.post(reverse("fitting:telemetry"), {
        "ship_type_id": RIFTER, "items": json.dumps(parsed["items"]), "skills": "allv",
        "tgt_sig": "40", "tgt_vel": "150", "tgt_distance": "5000", "tgt_ss": "20",
        "tgt_sensor": "gravimetric", "tgt_hp": "5000", "warp_distance_au": "20",
        "dmg_em": "100", "dmg_thermal": "0", "dmg_kinetic": "0", "dmg_explosive": "0"})
    assert resp.status_code == 200
    assert b"Applied" in resp.content


# --------------------------------------------------------------------------- #
# API-11: applied-DPS display (per-weapon + totals)
# --------------------------------------------------------------------------- #
def test_applied_dps_panel_renders(dogma):
    items = [{"type_id": AC, "slot": "high", "state": "active", "charge_type_id": FUSION, "quantity": 1}]
    op = services.operating_profile(target={"signature_radius": 40, "velocity": 100,
                                            "distance_m": 3000})
    telem = services.evaluate(RIFTER, items, SkillProfile.omniscient(), op)
    html = render_to_string("fitting/_telemetry.html",
                            {"telemetry": telem, "show_skills": False})
    assert "Applied" in html          # applied totals row
    assert "Sust." in html            # per-weapon sustained column
    assert "200mm AutoCannon I" in html


# --------------------------------------------------------------------------- #
# API-10 mode / API-6/7/8/10/12 panels via synthetic telemetry sections
# --------------------------------------------------------------------------- #
def _base_telem(dogma_items=None):
    items = dogma_items or [{"type_id": DC, "slot": "low", "state": "active",
                             "charge_type_id": None, "quantity": 1}]
    return services.evaluate(RIFTER, items, SkillProfile.omniscient())


def test_mining_section_renders(dogma):
    telem = _base_telem()
    telem["industry"] = {"m3_per_hour_total": 1200.0, "by_kind": {"ore": 1200.0},
                         "modules": [{"type_id": 1, "name": "Miner II", "kind": "ore",
                                      "yield_per_cycle": 80.0, "cycle_s": 60.0, "m3_per_hour": 600.0}]}
    html = render_to_string("fitting/_telemetry.html", {"telemetry": telem, "show_skills": False})
    assert "Mining" in html and "Miner II" in html and "1,200" in html


def test_ewar_section_jam_and_on_target(dogma):
    telem = _base_telem()
    telem["ewar"] = {
        "count": 1, "modules": [{"type_id": 1, "name": "Test Jammer", "kind": "ecm",
                                 "strengths": {}, "optimal_m": 24000, "falloff_m": 0,
                                 "jam_chance": 0.5}],
        "jam": {"combined_chance": 0.5, "jammer_count": 1, "reason": None,
                "target_sensor_strength": 20, "target_sensor_type": "radar"},
        "ewar_on_target": {"base": {"signature": 100, "velocity": 300},
                           "adjusted": {"signature": 150, "velocity": 150},
                           "painter_sig_pct": 50.0, "web_velocity_pct": -50.0}}
    html = render_to_string("fitting/_telemetry.html", {"telemetry": telem, "show_skills": False})
    assert "Electronic warfare" in html
    assert "Combined jam chance" in html
    assert "On target" in html


def test_fighters_section_renders(dogma):
    telem = _base_telem()
    telem["fighters"] = {
        "squadrons": [{"type_id": 1, "name": "Templar II", "role": "light", "count": 6,
                       "squadron_dps": 300.0, "applied_dps": None}],
        "totals": {"fighter_dps": 300.0, "volley": 100.0, "tubes_used": 1, "tubes_total": 3,
                   "bay_used_m3": 100.0, "bay_capacity_m3": 500.0, "role_slots": {}}}
    html = render_to_string("fitting/_telemetry.html", {"telemetry": telem, "show_skills": False})
    assert "Fighters" in html and "Templar II" in html and "Tubes" in html


def test_projected_and_boosts_sections_render(dogma):
    telem = _base_telem()
    telem["projected"] = {"count": 1, "modules": [
        {"type_id": 1, "name": "Hostile Web", "state": "active", "quantity": 2,
         "effect_summary": "50% max velocity"}]}
    telem["boosts"] = {"count": 1, "boosts": [
        {"charge_type_id": 1, "name": "Shield Harmonizing Charge",
         "buffs": [{"buff_id": 10, "strength_pct": 30.0, "applied": True}]}]}
    telem["capacitor"]["incoming_pressure"] = 12.5
    telem["defence"]["incoming_rep"] = {"shield_hps": 100.0, "armor_hps": 0.0,
                                        "hull_hps": 0.0, "total_hps": 100.0}
    html = render_to_string("fitting/_telemetry.html", {"telemetry": telem, "show_skills": False})
    assert "Projected on us" in html and "Hostile Web" in html
    assert "Fleet boosts" in html and "Shield Harmonizing Charge" in html
    assert "Incoming neut" in html and "Incoming remote rep" in html


def test_ship_mode_lock_and_warp_rows(dogma):
    telem = _base_telem()
    telem["ship"]["mode"] = {"type_id": 1, "name": "Svipul Defense Mode"}
    telem["targeting"]["lock_time_s"] = 3.2
    telem["mobility"]["warp_time_s"] = 12.5
    html = render_to_string("fitting/_telemetry.html", {"telemetry": telem, "show_skills": False})
    assert "Tactical mode" in html and "Svipul Defense Mode" in html
    assert "Lock time" in html and "Warp time" in html


def test_range_formatting_applied(dogma):
    # Rifter max_target_range is 20000 m → rendered as "20 km" by the rangem filter.
    telem = _base_telem()
    html = render_to_string("fitting/_telemetry.html", {"telemetry": telem, "show_skills": False})
    assert "20 km" in html


# --------------------------------------------------------------------------- #
# WS-5 modes / WS-7 boosts editor scaffolding + service helpers
# --------------------------------------------------------------------------- #
def test_ship_tactical_modes_service_and_editor(client, owner, dogma):
    _seed_extra_types()
    modes = services.ship_tactical_modes(RIFTER)
    assert any(m["type_id"] == 81004 for m in modes)
    fit = services.create_fit(owner, name="Mode", ship_type_id=RIFTER, items=[])
    client.force_login(owner)
    body = client.get(reverse("fitting:detail", args=[fit.pk])).content.decode()
    assert "Tactical mode" in body and "Rifter Defense Mode" in body


def test_command_burst_charges_service_and_editor(client, owner, dogma):
    _seed_extra_types()
    charges = services.command_burst_charges()
    assert any(c["type_id"] == 81003 for c in charges)
    # Blueprints share the group-name pattern but carry no warfareBuff1ID — excluded.
    assert not any(c["name"].endswith("Blueprint") for c in charges)
    fit = services.create_fit(owner, name="Boost", ship_type_id=RIFTER, items=[])
    client.force_login(owner)
    body = client.get(reverse("fitting:detail", args=[fit.pk])).content.decode()
    assert "Fleet boosts" in body and "Shield Harmonizing Charge" in body


def test_mode_item_persists_and_evaluates(client, owner, dogma):
    _seed_extra_types()
    fit = services.create_fit(owner, name="M", ship_type_id=RIFTER, items=[])
    client.force_login(owner)
    items = [{"type_id": 81004, "slot": "mode", "state": "active",
              "charge_type_id": None, "quantity": 1}]
    r = client.post(reverse("fitting:save", args=[fit.pk]),
                    {"ship_type_id": RIFTER, "items": json.dumps(items)},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r.status_code == 200
    fit.refresh_from_db()
    assert any(i.get("slot") == "mode" for i in fit.current_revision.items)


# --------------------------------------------------------------------------- #
# WS-6 projected search + WS-11 mutated attrs endpoints
# --------------------------------------------------------------------------- #
def test_search_projected_scopes_to_ewar(client, owner, dogma):
    _seed_extra_types()
    client.force_login(owner)
    r = client.get(reverse("fitting:search_projected"), {"q": "Test ECM"})
    ids = {res["type_id"] for res in r.json()["results"]}
    assert 81005 in ids
    for res in r.json()["results"]:
        assert res["slot"] == "projected"
    # A non-ewar module (the seeded AutoCannon) must NOT be offered as projectable.
    r2 = client.get(reverse("fitting:search_projected"), {"q": "AutoCannon"})
    assert AC not in {res["type_id"] for res in r2.json()["results"]}


def test_module_attrs_endpoint(client, owner, dogma):
    from apps.sde.models import SdeDogmaAttribute
    # The mutated editor resolves attribute names from the dogma-attribute table (fully imported
    # in prod); seed the AutoCannon's damageMultiplier name for this minimal fixture.
    SdeDogmaAttribute.objects.get_or_create(attribute_id=64, defaults={"name": "damageMultiplier"})
    client.force_login(owner)
    r = client.get(reverse("fitting:module_attrs"), {"type_id": AC})
    attrs = r.json()["results"]
    assert attrs and all("name" in a and "attribute_id" in a for a in attrs)
    # The AutoCannon's damageMultiplier (attr 64) is offered for mutation.
    assert any(a["attribute_id"] == 64 for a in attrs)


def test_search_modules_slot_broadens_categories(client, owner, dogma):
    _seed_extra_types()
    client.force_login(owner)
    r = client.get(reverse("fitting:search_modules"), {"q": "Ocular", "slot": "implant"})
    results = r.json()["results"]
    assert any(res["type_id"] == 81000 for res in results)
    assert all(res["slot"] == "implant" for res in results)


def test_mutated_overrides_persist_and_evaluate(client, owner, dogma):
    fit = services.create_fit(owner, name="Mut", ship_type_id=RIFTER, items=[])
    client.force_login(owner)
    items = [{"type_id": AC, "slot": "high", "state": "active", "charge_type_id": FUSION,
              "quantity": 1, "attr_overrides": {"64": 2.5}}]
    r = client.post(reverse("fitting:save", args=[fit.pk]),
                    {"ship_type_id": RIFTER, "items": json.dumps(items)},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r.status_code == 200
    fit.refresh_from_db()
    stored = fit.current_revision.items[0]
    assert stored.get("attr_overrides") == {"64": 2.5}
    # And the editor exposes the mutate affordance + wires the attribute endpoint.
    body = client.get(reverse("fitting:detail", args=[fit.pk])).content.decode()
    assert "Mutate" in body
    assert reverse("fitting:module_attrs") in body
