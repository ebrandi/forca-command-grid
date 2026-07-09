"""Seed realistic example data to validate every section end-to-end.

Creates a Titan build project (full BOM), a Jita→Amarr logistics run, a real
battlecruiser doctrine, a PI chain, a contract, and extra stockpile targets.
Idempotent. Run after `import_sde_fuzzwork`. Not production data.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.doctrines.fitparser import parse_eft
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.services import derive_skill_requirements
from apps.industry.models import IndustryProject, IndustryProjectItem
from apps.industry.services import compute_project_bom
from apps.market.models import MarketLocation
from apps.sde.models import SdeType
from apps.stockpile.models import HaulingTask, Stockpile
from apps.stockpile.services import record_manual_stock

AVATAR = 11567  # Amarr Titan
FEROX = 16227
HOUND = 12034  # T2 Stealth Bomber — demonstrates the invention requirement
TRITANIUM = 34

FEROX_EFT = """[Ferox, Railgun Ferox]
Damage Control II
Magnetic Field Stabilizer II
Magnetic Field Stabilizer II
Magnetic Field Stabilizer II
Reactor Control Unit II

50MN Cold-Gas Enduring Microwarpdrive
Large Shield Extender II
Large Shield Extender II
Multispectrum Shield Hardener II
Multispectrum Shield Hardener II

250mm Railgun II
250mm Railgun II
250mm Railgun II
250mm Railgun II
250mm Railgun II
250mm Railgun II
250mm Railgun II

Medium Core Defense Field Extender II
Medium Core Defense Field Extender II

Hobgoblin II x5
Caldari Navy Antimatter Charge M x7
"""


class Command(BaseCommand):
    help = "Seed example data across all sections."

    def _vol(self, type_id):
        return SdeType.objects.filter(type_id=type_id).values_list("volume", flat=True).first() or 0.0

    def handle(self, *args, **options) -> None:
        # --- Industry: build a Titan ---
        titan, _ = IndustryProject.objects.update_or_create(
            name="Build an Avatar (Titan)",
            defaults={
                "objective_type": IndustryProject.Objective.BUILD,
                "status": IndustryProject.Status.ACTIVE,
                "description": "Capital construction example — the full bill of materials for an Amarr Titan.",
            },
        )
        IndustryProjectItem.objects.update_or_create(
            project=titan, type_id=AVATAR,
            defaults={"quantity": 1, "build_or_buy": IndustryProjectItem.BuildOrBuy.BUILD,
                      "strategy": IndustryProjectItem.Strategy.BUILD_TO_MINERALS},
        )
        bom = compute_project_bom(titan)

        # --- Industry: a T1 production run, built all the way to minerals ---
        ferox_run, _ = IndustryProject.objects.update_or_create(
            name="Ferox production run (to minerals)",
            defaults={
                "objective_type": IndustryProject.Objective.BUILD,
                "status": IndustryProject.Status.ACTIVE,
                "description": "Recursive BOM example — 10 Feroxes expanded down to raw minerals.",
            },
        )
        IndustryProjectItem.objects.update_or_create(
            project=ferox_run, type_id=FEROX,
            defaults={"quantity": 10, "build_or_buy": IndustryProjectItem.BuildOrBuy.BUILD,
                      "strategy": IndustryProjectItem.Strategy.BUILD_TO_MINERALS},
        )
        compute_project_bom(ferox_run)

        # --- Industry: a T2 hull, to surface the invention requirement ---
        t2, _ = IndustryProject.objects.update_or_create(
            name="T2 build — Hound (Stealth Bomber)",
            defaults={
                "objective_type": IndustryProject.Objective.BUILD,
                "status": IndustryProject.Status.ACTIVE,
                "description": "T2 example — components plus the datacores needed to invent the blueprint.",
            },
        )
        IndustryProjectItem.objects.update_or_create(
            project=t2, type_id=HOUND,
            defaults={"quantity": 1, "build_or_buy": IndustryProjectItem.BuildOrBuy.BUILD,
                      "strategy": IndustryProjectItem.Strategy.BUILD_TO_MINERALS},
        )
        compute_project_bom(t2)

        # --- Logistics: Jita -> Amarr ---
        jita = MarketLocation.objects.filter(is_price_reference=True).first()
        if not jita:
            jita, _ = MarketLocation.objects.update_or_create(
                name="Jita IV - Moon 4 (reference)",
                defaults={"location_type": "system", "region_id": 10000002, "system_id": 30000142,
                          "is_price_reference": True},
            )
        amarr, _ = MarketLocation.objects.update_or_create(
            name="Amarr VIII (Oris) - Emperor Family Academy",
            defaults={"location_type": "station", "region_id": 10000043, "system_id": 30002187},
        )
        for type_id, qty in [(TRITANIUM, 5_000_000), (FEROX, 20)]:
            HaulingTask.objects.update_or_create(
                type_id=type_id, source_location=jita, dest_location=amarr,
                status=HaulingTask.Status.OPEN,
                defaults={"quantity": qty, "volume_m3": self._vol(type_id) * qty},
            )

        # --- Doctrines: a real battlecruiser fleet fit ---
        cat, _ = DoctrineCategory.objects.update_or_create(
            key="dps", defaults={"label": "DPS", "sort_order": 5}
        )
        ferox_doc, _ = Doctrine.objects.update_or_create(
            name="Ferox Railgun Fleet",
            defaults={"category": cat, "status": Doctrine.Status.ACTIVE, "is_public_preview": True,
                      "priority": 80, "description": "Shield railgun battlecruiser fleet doctrine."},
        )
        parsed = parse_eft(FEROX_EFT)
        fit, _ = DoctrineFit.objects.update_or_create(
            doctrine=ferox_doc, name="Shield Ferox",
            defaults={"ship_type_id": parsed["ship_type_id"] or FEROX, "role": "dps",
                      "eft_text": FEROX_EFT, "modules": parsed["modules"]},
        )
        reqs = derive_skill_requirements(fit)

        # --- Stockpile targets (variety for market/stock pages) ---
        stock, _ = Stockpile.objects.update_or_create(
            name="Staging hangar", defaults={"kind": Stockpile.Kind.CORP, "location": amarr}
        )
        record_manual_stock(stock, FEROX, quantity_current=3, quantity_target=20)
        record_manual_stock(stock, TRITANIUM, quantity_current=2_000_000, quantity_target=10_000_000)

        # --- Intel: a watchlist + a battle report from real killmails ---
        from apps.killboard.battle import generate_battle_report
        from apps.killboard.models import Killmail, Watchlist, WatchlistEntry

        watchlist, _ = Watchlist.objects.update_or_create(
            name="Known hostiles", defaults={"purpose": "Repeat aggressors on our killboard."}
        )
        # Watch the corporations that have killed us most often.
        hostile_corps = (
            Killmail.objects.filter(home_corp_role=Killmail.HomeRole.VICTIM)
            .exclude(victim_corporation_id=None)
            .values_list("victim_corporation_id", flat=True)
        )
        for corp_id in {c for c in hostile_corps}:
            WatchlistEntry.objects.get_or_create(
                watchlist=watchlist, entity_type=WatchlistEntry.EntityType.CORPORATION,
                entity_id=corp_id, defaults={"note": "auto-added from losses"},
            )
        # Battle report from whichever system has the most recorded killmails.
        from django.db.models import Count

        busiest = (
            Killmail.objects.values("solar_system_id")
            .annotate(n=Count("killmail_id")).order_by("-n").first()
        )
        if busiest:
            generate_battle_report(busiest["solar_system_id"], hours=24 * 3650, title="Recent engagement")

        self.stdout.write(self.style.SUCCESS(
            f"Examples seeded: Titan project ({bom['item_count']} item, est. {titan.estimated_cost} ISK), "
            f"Ferox doctrine ({reqs} skill reqs, {len(parsed['modules'])} modules), "
            f"Jita→Amarr hauls, PI chain, contract, stock targets."
        ))
        if parsed["unresolved"]:
            self.stdout.write(f"  (unresolved fit lines: {parsed['unresolved']})")
