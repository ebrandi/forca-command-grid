"""Import a corporation's or character's killmails from zKillboard.

Usage:
    manage.py import_zkill --corp 98493095
    manage.py import_zkill --character 344805695
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.killboard.tasks import import_from_zkill


class Command(BaseCommand):
    help = "Enrich the killboard from zKillboard (optional supplementary source)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--corp", type=int, help="corporation_id")
        parser.add_argument("--character", type=int, help="character_id")

    def handle(self, *args, **options) -> None:
        if options.get("corp"):
            n = import_from_zkill("corporation", options["corp"])
            self.stdout.write(self.style.SUCCESS(f"Ingested {n} killmail(s) for corp {options['corp']}."))
        elif options.get("character"):
            n = import_from_zkill("character", options["character"])
            self.stdout.write(
                self.style.SUCCESS(f"Ingested {n} killmail(s) for character {options['character']}.")
            )
        else:
            raise CommandError("Provide --corp <id> or --character <id>.")
