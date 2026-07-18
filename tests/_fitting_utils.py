"""Shared helpers for the Tocha's Lab (fitting) test suite.

A small, self-contained fixture built from real EVE type ids (so EFT name resolution and
the dogma engine exercise the real code paths) — original data, nothing copied from an
external fit library. Plain functions in the ``_campaign_utils`` style; each test module
wraps them in its own thin pytest fixtures.
"""
from __future__ import annotations

# Real type ids, so name resolution / search hit the same paths production does.
RIFTER, AC, FUSION, DC = 587, 484, 192, 2046
MINFRIG, GUNNERY, SPT = 3331, 3300, 3320

EFT = ("[Rifter, Test Rifter]\n200mm AutoCannon I, Fusion S\n"
       "200mm AutoCannon I, Fusion S\n\nDamage Control I")


def seed_dogma():
    """Seed the minimal SDE + dogma data the engine needs to fully evaluate a Rifter."""
    from apps.admin_audit.models import AppSetting
    from apps.fitting.engine import attributes as A
    from apps.market.models import MarketPrice
    from apps.sde.models import (
        SdeCategory,
        SdeGroup,
        SdeShipBonus,
        SdeType,
        SdeTypeAttribute,
        SdeTypeEffect,
        SdeTypeSkill,
    )
    R, AR, HR = A.SHIELD_RESONANCE, A.ARMOR_RESONANCE, A.HULL_RESONANCE
    for cid, name in [(6, "Ship"), (7, "Module"), (8, "Charge"), (16, "Skill")]:
        SdeCategory.objects.get_or_create(category_id=cid, defaults={"name": name})
    for gid, cid, name in [(25, 6, "Frigate"), (55, 7, "Projectile Turret"), (60, 7, "Damage Control"),
                           (83, 8, "Ammo"), (349, 16, "Skill")]:
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": cid, "name": name})
    types = {
        RIFTER: ("Rifter", 25, {
            A.CPU_OUTPUT: 125, A.POWER_OUTPUT: 41, A.CALIBRATION: 400,
            A.HI_SLOTS: 4, A.MED_SLOTS: 3, A.LOW_SLOTS: 3, A.RIG_SLOTS: 3,
            A.TURRET_HARDPOINTS: 3, A.LAUNCHER_HARDPOINTS: 1,
            A.SHIELD_HP: 450, A.ARMOR_HP: 400, A.HULL_HP: 350,
            R["em"]: 1.0, R["thermal"]: 0.84, R["kinetic"]: 0.6, R["explosive"]: 0.5,
            AR["em"]: 0.5, AR["thermal"]: 0.55, AR["kinetic"]: 0.75, AR["explosive"]: 0.9,
            HR["em"]: 1.0, HR["thermal"]: 1.0, HR["kinetic"]: 1.0, HR["explosive"]: 1.0,
            A.CAP_CAPACITY: 330, A.CAP_RECHARGE_RATE: 187500, A.MASS: 1067000, A.AGILITY: 2.9,
            A.MAX_VELOCITY: 355, A.SIGNATURE_RADIUS: 35, A.WARP_SPEED_MULT: 5.0,
            A.MAX_TARGET_RANGE: 20000, A.MAX_LOCKED_TARGETS: 5, A.SCAN_RESOLUTION: 730,
            A.SENSOR_STRENGTHS["gravimetric"]: 11, A.CAPACITY_CARGO: 140,
        }),
        AC: ("200mm AutoCannon I", 55, {A.CPU_USAGE: 3, A.POWER_USAGE: 6, A.DAMAGE_MULTIPLIER: 1.0,
             A.RATE_OF_FIRE: 2475, A.OPTIMAL_RANGE: 1200, A.FALLOFF: 7500, A.TRACKING_SPEED: 0.198}),
        FUSION: ("Fusion S", 83, {A.EXPLOSIVE_DAMAGE: 8.8}),
        DC: ("Damage Control I", 60, {A.CPU_USAGE: 5, A.POWER_USAGE: 1,
             R["em"]: 0.875, R["thermal"]: 0.875, R["kinetic"]: 0.875, R["explosive"]: 0.875,
             AR["em"]: 0.85, AR["thermal"]: 0.85, AR["kinetic"]: 0.85, AR["explosive"]: 0.85,
             HR["em"]: 0.5, HR["thermal"]: 0.5, HR["kinetic"]: 0.5, HR["explosive"]: 0.5}),
    }
    for tid, (name, gid, attrs) in types.items():
        SdeType.objects.get_or_create(type_id=tid, defaults={"group_id": gid, "name": name})
        SdeTypeAttribute.objects.bulk_create(
            [SdeTypeAttribute(type_id=tid, attribute_id=k, value=v) for k, v in attrs.items()],
            ignore_conflicts=True)
        MarketPrice.objects.get_or_create(
            type_id=tid, location=None, profile=MarketPrice.Profile.JITA_SELL,
            defaults={"sell_min": 1000})
    SdeTypeEffect.objects.bulk_create([
        SdeTypeEffect(type_id=AC, effect_id=A.EFFECT_HI_POWER, is_default=True),
        SdeTypeEffect(type_id=DC, effect_id=A.EFFECT_LO_POWER, is_default=True),
    ], ignore_conflicts=True)
    for sid, name in [(MINFRIG, "Minmatar Frigate"), (GUNNERY, "Gunnery"), (SPT, "Small Projectile Turret")]:
        SdeType.objects.get_or_create(type_id=sid, defaults={"group_id": 349, "name": name})
    SdeTypeSkill.objects.get_or_create(type_id=RIFTER, skill_type_id=MINFRIG, defaults={"level": 1})
    SdeTypeSkill.objects.get_or_create(type_id=AC, skill_type_id=GUNNERY, defaults={"level": 1})
    SdeTypeSkill.objects.get_or_create(type_id=AC, skill_type_id=SPT, defaults={"level": 1})
    SdeShipBonus.objects.get_or_create(
        ship_type_id=RIFTER, key="minfrig_dmg",
        defaults={"target_attribute_id": A.DAMAGE_MULTIPLIER, "amount": 5.0, "per_level": True,
                  "skill_type_id": MINFRIG, "match_group_ids": [55], "label": "Minmatar Frigate"})
    AppSetting.objects.update_or_create(key="dogma_data_version", defaults={"value": {"version": "test"}})
    return True


def make_member(username, char_id, name, role=None):
    """A corp-member user (+ main character), optionally holding an extra role."""
    from django.contrib.auth import get_user_model

    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role
    from core import rbac
    User = get_user_model()
    u = User.objects.create(username=username, first_name=name)
    u.set_unusable_password()
    u.save()
    EveCharacter.objects.create(character_id=char_id, user=u, name=name, is_main=True,
                                is_corp_member=True, is_corp_director=(role == rbac.ROLE_DIRECTOR))
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    if role:
        RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u
