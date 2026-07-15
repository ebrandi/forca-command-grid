"""Seed demo data for development/QA: roles, home corp, a doctrine, locations.

Idempotent. Run after `load_sde`. Not for production data.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.corporation.models import EveCorporation
from apps.doctrines.fitparser import parse_eft
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.services import derive_skill_requirements
from apps.market.models import MarketLocation
from apps.sso.services import ensure_role
from apps.stockpile.models import Stockpile
from apps.stockpile.services import record_manual_stock
from core import rbac

RIFTER_EFT = """[Rifter, Newbro Tackle]
200mm AutoCannon I
200mm AutoCannon I
200mm AutoCannon I
Damage Control I
Fusion S x100
"""


class Command(BaseCommand):
    help = "Seed demo data (roles, home corp, a doctrine, market locations)."

    def handle(self, *args, **options) -> None:
        for key in (rbac.ROLE_MEMBER, rbac.ROLE_OFFICER, rbac.ROLE_DIRECTOR, rbac.ROLE_ADMIN):
            ensure_role(key)

        corp_id = settings.FORCA_HOME_CORP_ID or 98000001
        EveCorporation.objects.update_or_create(
            corporation_id=corp_id,
            defaults={"name": "Forças Armadas", "ticker": "FORCA", "is_home_corp": True},
        )

        cat, _ = DoctrineCategory.objects.update_or_create(
            key="newbro", defaults={"label": "Newbro", "sort_order": 1}
        )
        doctrine, _ = Doctrine.objects.update_or_create(
            name="Newbro Tackle",
            defaults={
                "category": cat,
                "description": "Cheap, fast tackle for new pilots.",
                "status": Doctrine.Status.ACTIVE,
                "is_public_preview": True,
                "priority": 100,
            },
        )
        parsed = parse_eft(RIFTER_EFT)
        fit, _ = DoctrineFit.objects.update_or_create(
            doctrine=doctrine,
            name=parsed["fit_name"],
            defaults={
                "ship_type_id": parsed["ship_type_id"] or 587,
                "role": "tackle",
                "eft_text": RIFTER_EFT,
                "modules": parsed["modules"],
                "is_cheap_alt": True,
            },
        )
        created = derive_skill_requirements(fit)

        MarketLocation.objects.update_or_create(
            name="Jita IV - Moon 4 (reference)",
            defaults={
                "location_type": MarketLocation.LocationType.SYSTEM,
                "region_id": 10000002,
                "system_id": 30000142,
                "is_price_reference": True,
            },
        )
        staging, _ = MarketLocation.objects.update_or_create(
            name="Staging",
            defaults={
                "location_type": MarketLocation.LocationType.SYSTEM,
                "region_id": 10000002,
                "system_id": 30002053,
                "is_staging": True,
            },
        )

        # Onboarding milestones + glossary are owned by migrations 0003 (canonical seed),
        # 0004 (glossary reconcile) and 0005 (milestone reconcile), and are fully translated.
        # Demo runs must NOT re-seed them: update_or_create with short demo text previously
        # clobbered the canonical, catalogue-matched content and left it rendering untranslated
        # in every locale (Doctrine/ISK/Highsec/Tackle and the link-character/import-skills
        # milestones). Onboarding content is seeded by migration, not by the demo command.

        # A corp stockpile with a target so dashboards show a shortfall.
        stockpile, _ = Stockpile.objects.update_or_create(
            name="Staging hangar", defaults={"kind": Stockpile.Kind.CORP, "location": staging}
        )
        record_manual_stock(stockpile, type_id=587, quantity_current=4, quantity_target=40)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded demo data (doctrine '{doctrine.name}', {created} skill reqs)."
            )
        )
