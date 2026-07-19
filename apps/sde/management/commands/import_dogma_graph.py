"""Canonical name for the CCP FSD dogma import (ship bonuses + modifier graph + skill dogma).

Thin alias of ``import_ship_bonuses`` (kept for backward compatibility with existing runbooks
that invoke the older name). Both commands do the identical full import — see
``import_ship_bonuses`` for the details and the ORDERING note (run after ``import_sde_fuzzwork``).
"""
from __future__ import annotations

from apps.sde.management.commands.import_ship_bonuses import Command as _ImportCommand


class Command(_ImportCommand):
    help = "Import the CCP FSD dogma graph: ship bonuses + modifier graph + skill dogma."
