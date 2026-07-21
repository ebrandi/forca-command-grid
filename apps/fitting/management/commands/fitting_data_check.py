"""Operator command: validate the fitting dataset end to end.

    manage.py fitting_data_check [--strict]

Verifies everything the Tocha's Lab engine v2 needs — presence, referential
integrity, modifier-semantics coverage, the documented client-internal data patches,
and a live sample calculation — and exits non-zero when a CRITICAL check fails
(``--strict`` also fails on warnings). Wire into deployment after every SDE import:
a full ``import_sde_fuzzwork`` run cascade-clears the ship-bonus/modifier layer, and
this command is what catches that state before users do.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

_KNOWN_FUNCS = {"ItemModifier", "LocationModifier", "LocationGroupModifier",
                "LocationRequiredSkillModifier", "OwnerRequiredSkillModifier",
                "EffectStopper"}
_KNOWN_OPS = {-1, 0, 1, 2, 3, 4, 5, 6, 7, 9}
_KNOWN_DOMAINS = {"", "shipID", "charID", "itemID", "otherID", "targetID", "target",
                  "structureID"}
_SKILL_CATEGORY = 16
_SHIP_CATEGORY = 6
# Published hulls legitimately without trait bonuses (shuttles, corvettes, special
# hulls). Above this many bonus-less hulls the graph import is stale or degraded.
_MAX_HULLS_WITHOUT_BONUS = 45
_PATCH_EFFECT_NAMES = ("selfRof", "missileEMDmgBonus", "missileExplosiveDmgBonus",
                       "missileKineticDmgBonus2", "missileThermalDmgBonus",
                       "droneDmgBonus",
                       # WS-6 projected-ewar synthesised modifiers (see
                       # import_ship_bonuses._CLIENT_INTERNAL_EFFECTS): web / painter /
                       # sensor-damp default effects ship empty modifierInfo in CCP's SDE.
                       "remoteWebifierFalloff", "remoteTargetPaintFalloff",
                       "remoteSensorDampFalloff")


class Command(BaseCommand):
    help = "Validate the Tocha's Lab fitting dataset (exit non-zero on critical failure)."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true",
                            help="Treat warnings as failures too.")

    def handle(self, *args, **options):
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self._check_presence()
        self._check_references()
        self._check_modifier_semantics()
        self._check_patches()
        self._check_versions()
        self._check_sample_calculation()

        for w in self.warnings:
            self.stdout.write(self.style.WARNING(f"WARN  {w}"))
        for f in self.failures:
            self.stdout.write(self.style.ERROR(f"FAIL  {f}"))
        ok = not self.failures and not (options["strict"] and self.warnings)
        if ok:
            self.stdout.write(self.style.SUCCESS(
                f"fitting_data_check OK ({len(self.warnings)} warnings)"))
            return
        raise SystemExit(1)

    # ------------------------------------------------------------------ #
    def _check_presence(self):
        from apps.sde.models import (
            SdeDogmaAttribute, SdeDogmaEffect, SdeModifier, SdeShipBonus,
            SdeType, SdeTypeAttribute, SdeTypeEffect,
        )

        for label, qs, minimum in (
            ("dogma attributes", SdeDogmaAttribute.objects, 2000),
            ("dogma effects", SdeDogmaEffect.objects, 2500),
            ("type attributes", SdeTypeAttribute.objects, 100_000),
            ("type effects", SdeTypeEffect.objects, 20_000),
            ("dogma modifiers (SdeModifier)", SdeModifier.objects, 3000),
            ("ship bonuses", SdeShipBonus.objects, 2500),
        ):
            n = qs.count()
            if n < minimum:
                self.failures.append(
                    f"{label}: {n} rows (expected >= {minimum}) — "
                    f"{'run import_dogma_graph' if 'modifier' in label or 'bonus' in label else 'run import_sde_fuzzwork'}")
        skills_with_dogma = (
            SdeTypeAttribute.objects.filter(
                type__group__category_id=_SKILL_CATEGORY)
            .values("type_id").distinct().count())
        if skills_with_dogma < 400:
            self.failures.append(
                f"skills with dogma: {skills_with_dogma} (expected ~590) — a full "
                f"import_sde_fuzzwork run wipes skill dogma; re-run import_dogma_graph")
        # Level-scaling machinery: skill-level (280) pre-multiplication modifiers.
        n280 = SdeModifier.objects.filter(modifying_attribute_id=280).count()
        if n280 < 400:
            self.failures.append(
                f"skill-level (attr 280) scaling modifiers: {n280} (expected ~500)")
        # invTypes-synthesised attrs (mass/capacity/volume) present on ships.
        cargo_rows = SdeTypeAttribute.objects.filter(
            attribute_id=38, type__group__category_id=_SHIP_CATEGORY).count()
        if cargo_rows < 300:
            self.failures.append(
                f"ship cargo capacity (attr 38): only {cargo_rows} hulls carry it — "
                f"re-run import_sde_fuzzwork (invTypes synthesis)")

    def _check_references(self):
        from apps.sde.models import (
            SdeDogmaAttribute, SdeDogmaEffect, SdeModifier, SdeTypeAttribute,
            SdeTypeEffect,
        )

        effect_ids = set(SdeDogmaEffect.objects.values_list("effect_id", flat=True))
        attr_ids = set(SdeDogmaAttribute.objects.values_list("attribute_id", flat=True))

        dangling_te = (SdeTypeEffect.objects.exclude(effect_id__in=effect_ids)
                       .values_list("effect_id", flat=True).distinct())
        if dangling_te:
            self.failures.append(
                f"type effects referencing unknown effects: {sorted(dangling_te)[:10]}")
        dangling_m = (SdeModifier.objects.exclude(effect_id__in=effect_ids)
                      .exclude(effect_id__lt=0)
                      .values_list("effect_id", flat=True).distinct())
        if dangling_m:
            self.failures.append(
                f"modifiers referencing unknown effects: {sorted(dangling_m)[:10]}")
        bad_attr = (SdeModifier.objects
                    .exclude(modified_attribute_id__in=attr_ids)
                    .exclude(modified_attribute_id__isnull=True)
                    .values_list("modified_attribute_id", flat=True).distinct())
        if bad_attr:
            self.failures.append(
                f"modifiers targeting unknown attributes: {sorted(bad_attr)[:10]}")
        bad_ta = (SdeTypeAttribute.objects.exclude(attribute_id__in=attr_ids)
                  .values_list("attribute_id", flat=True).distinct())
        if bad_ta:
            self.warnings.append(
                f"type attributes with no attribute definition: {sorted(bad_ta)[:10]}")

    def _check_modifier_semantics(self):
        from apps.sde.models import SdeGroup, SdeModifier, SdeShipBonus, SdeType

        funcs = set(SdeModifier.objects.values_list("func", flat=True).distinct())
        unknown_funcs = funcs - _KNOWN_FUNCS
        if unknown_funcs:
            self.failures.append(
                f"UNKNOWN modifier funcs (engine would drop them): {sorted(unknown_funcs)}")
        ops = set(SdeModifier.objects.exclude(operation__isnull=True)
                  .values_list("operation", flat=True).distinct())
        unknown_ops = ops - _KNOWN_OPS
        if unknown_ops:
            self.failures.append(
                f"UNKNOWN modifier operations: {sorted(unknown_ops)}")
        domains = set(SdeModifier.objects.values_list("domain", flat=True).distinct())
        unknown_domains = domains - _KNOWN_DOMAINS
        if unknown_domains:
            self.warnings.append(
                f"unclassified modifier domains: {sorted(unknown_domains)}")

        hulls_no_bonus = (
            SdeType.objects.filter(group__category_id=_SHIP_CATEGORY, published=True)
            .exclude(type_id__in=SdeShipBonus.objects.values_list("ship_type_id", flat=True))
            .count())
        if hulls_no_bonus > _MAX_HULLS_WITHOUT_BONUS:
            self.failures.append(
                f"{hulls_no_bonus} published hulls have no ship-bonus rows "
                f"(allowance {_MAX_HULLS_WITHOUT_BONUS}) — the dogma graph import is "
                f"stale relative to the type data; run import_dogma_graph")
        if not SdeGroup.objects.filter(group_id=645).exists():
            self.warnings.append("group 645 (Drone Damage Modules) missing")

    def _check_patches(self):
        from apps.sde.models import SdeDogmaEffect, SdeModifier

        for name in _PATCH_EFFECT_NAMES:
            row = SdeDogmaEffect.objects.filter(name=name).values_list(
                "effect_id", flat=True).first()
            if row is None:
                self.warnings.append(f"client-internal effect '{name}' not in dogma effects")
                continue
            if not SdeModifier.objects.filter(effect_id=row).exists():
                self.failures.append(
                    f"client-internal effect '{name}' ({row}) has no synthesised "
                    f"modifiers — run the current import_dogma_graph")

    def _check_versions(self):
        from apps.admin_audit.models import AppSetting

        rows = dict(AppSetting.objects.filter(
            key__in=("sde_version", "dogma_data_version", "ship_bonus_data_version",
                     "dogma_graph_version")).values_list("key", "value"))
        for key in ("sde_version", "dogma_data_version", "ship_bonus_data_version",
                    "dogma_graph_version"):
            if not (rows.get(key) or {}).get("version"):
                self.failures.append(f"data-version record missing: {key}")

    def _check_sample_calculation(self):
        from apps.fitting.engine.adapter import FittingEngine
        from apps.fitting.engine.types import FitInput, ModuleInput, SkillProfile, SlotKind
        from apps.sde.models import SdeType

        def tid(name):
            return (SdeType.objects.filter(name__iexact=name)
                    .values_list("type_id", flat=True).first())

        rifter = tid("Rifter")
        if not rifter:
            self.warnings.append("sample calc skipped: Rifter not in DB")
            return
        engine = FittingEngine()
        res = engine.evaluate(FitInput(ship_type_id=rifter), SkillProfile.omniscient())
        t = res.telemetry
        if res.status.value not in ("valid", "warnings") \
                or (t.get("defence", {}).get("ehp_total") or 0) <= 0 \
                or (t.get("capacitor", {}).get("capacity") or 0) <= 0:
            self.failures.append(
                f"sample calculation failed: status={res.status.value} "
                f"errors={res.errors} ehp={t.get('defence', {}).get('ehp_total')}")
        caracal, launcher, missile = tid("Caracal"), \
            tid("Heavy Missile Launcher II"), tid("Scourge Heavy Missile")
        if caracal and launcher and missile:
            res2 = engine.evaluate(
                FitInput(ship_type_id=caracal, modules=(
                    ModuleInput(type_id=launcher, slot=SlotKind.HIGH,
                                charge_type_id=missile),)),
                SkillProfile.omniscient())
            dps = (res2.telemetry.get("offence") or {}).get("missile_dps") or 0
            if dps <= 0:
                self.failures.append(
                    "sample calculation failed: Caracal HML fit computes 0 missile DPS")
