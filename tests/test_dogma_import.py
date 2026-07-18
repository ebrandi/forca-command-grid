"""Full dogma import from the Fuzzwork SQLite dump.

Exercises the import_sde_fuzzwork dogma loaders against a tiny synthetic SQLite (no
2 GB download), including the fittable-category bounding that keeps the per-type tables
from ballooning to every attribute of every type in the game.
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.admin_audit.models import AppSetting
from apps.sde.management.commands.import_sde_fuzzwork import Command
from apps.sde.models import (
    SdeCategory,
    SdeDogmaAttribute,
    SdeDogmaEffect,
    SdeGroup,
    SdeType,
    SdeTypeAttribute,
    SdeTypeEffect,
)

CPU, DMG_MULT, SKILL_RANK = 50, 64, 275
HI_POWER = 12


@pytest.fixture
def fuzzwork_sqlite():
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE dgmAttributeTypes (attributeID INT, attributeName TEXT, unitID INT,
            stackable INT, highIsGood INT, defaultValue REAL, published INT);
        CREATE TABLE dgmEffects (effectID INT, effectName TEXT, effectCategory INT,
            isOffensive INT, isAssistance INT, dischargeAttributeID INT, durationAttributeID INT,
            rangeAttributeID INT, falloffAttributeID INT, trackingSpeedAttributeID INT);
        CREATE TABLE dgmTypeAttributes (typeID INT, attributeID INT, valueInt INT, valueFloat REAL);
        CREATE TABLE dgmTypeEffects (typeID INT, effectID INT, isDefault INT);
        """
    )
    con.executemany("INSERT INTO dgmAttributeTypes VALUES (?,?,?,?,?,?,?)", [
        (CPU, "cpu", None, 1, 0, 0.0, 1),
        (DMG_MULT, "damageMultiplier", None, 0, 1, 1.0, 1),
    ])
    con.executemany("INSERT INTO dgmEffects VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (HI_POWER, "hiPower", 0, 0, 0, None, None, None, None, None),
    ])
    con.executemany("INSERT INTO dgmTypeAttributes VALUES (?,?,?,?)", [
        (587, CPU, 125, None),          # ship (fittable) — valueInt
        (484, DMG_MULT, None, 1.35),    # module (fittable) — valueFloat
        (3300, SKILL_RANK, 3, None),    # skill (NOT fittable) — must be excluded
    ])
    con.executemany("INSERT INTO dgmTypeEffects VALUES (?,?,?)", [
        (484, HI_POWER, 1),
        (3300, HI_POWER, 0),            # skill (NOT fittable) — must be excluded
    ])
    con.commit()
    return con


def test_full_dogma_import(db, fuzzwork_sqlite):
    for cid, name in [(6, "Ship"), (7, "Module"), (16, "Skill")]:
        SdeCategory.objects.create(category_id=cid, name=name)
    SdeGroup.objects.create(group_id=25, category_id=6, name="Frigate")
    SdeGroup.objects.create(group_id=55, category_id=7, name="Turret")
    SdeGroup.objects.create(group_id=255, category_id=16, name="Gunnery")
    SdeType.objects.create(type_id=587, group_id=25, name="Rifter")
    SdeType.objects.create(type_id=484, group_id=55, name="200mm AutoCannon I")
    SdeType.objects.create(type_id=3300, group_id=255, name="Gunnery")

    Command()._load_dogma_reference(fuzzwork_sqlite)

    # Definitions loaded for every attribute/effect.
    assert SdeDogmaAttribute.objects.get(attribute_id=CPU).name == "cpu"
    assert SdeDogmaAttribute.objects.get(attribute_id=DMG_MULT).stackable is False  # highIsGood/stackable mapped
    assert SdeDogmaEffect.objects.get(effect_id=HI_POWER).name == "hiPower"

    # Per-type values loaded for fittable types, with valueInt/valueFloat coalesced.
    assert SdeTypeAttribute.objects.get(type_id=587, attribute_id=CPU).value == 125.0
    assert SdeTypeAttribute.objects.get(type_id=484, attribute_id=DMG_MULT).value == pytest.approx(1.35)
    assert SdeTypeEffect.objects.get(type_id=484, effect_id=HI_POWER).is_default is True

    # The skill type (category 16) is NOT fittable — its attributes/effects are excluded.
    assert not SdeTypeAttribute.objects.filter(type_id=3300).exists()
    assert not SdeTypeEffect.objects.filter(type_id=3300).exists()

    # Data version stamped so the engine cache invalidates on refresh.
    assert AppSetting.objects.filter(key="dogma_data_version").exists()


def test_dogma_import_is_idempotent(db, fuzzwork_sqlite):
    SdeCategory.objects.create(category_id=6, name="Ship")
    SdeGroup.objects.create(group_id=25, category_id=6, name="Frigate")
    SdeType.objects.create(type_id=587, group_id=25, name="Rifter")
    cmd = Command()
    cmd._load_dogma_reference(fuzzwork_sqlite)
    cmd._load_dogma_reference(fuzzwork_sqlite)  # re-run must not duplicate
    assert SdeTypeAttribute.objects.filter(type_id=587, attribute_id=CPU).count() == 1
    assert SdeDogmaAttribute.objects.filter(attribute_id=CPU).count() == 1
