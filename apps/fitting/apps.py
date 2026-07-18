"""Tocha's Lab — the ship-fitting workspace & simulation feature.

The Django app is named generically (``fitting``); "Tocha's Lab" is the
user-facing brand, applied only in templates and localised strings. Keeping the
internal name generic avoids competitor/branding leakage into code, URLs and
migrations (see docs/architecture/decisions/tochas-lab-fitting-engine.md).
"""
from __future__ import annotations

from django.apps import AppConfig


class FittingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.fitting"
    label = "fitting"
    verbose_name = "Tocha's Lab (ship fitting)"
