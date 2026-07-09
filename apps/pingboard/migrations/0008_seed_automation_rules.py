"""Seed disabled automation rules for the documented trigger sources.

Every rule ships ``enabled=False`` — zero behaviour change until a director arms it on
the Admin Console. The threshold-swept sources (structure/moon/industry) become live the
moment they're enabled; the event-driven ones fire from the hooks wired into SRP /
logistics / store. Create-only + reversible (removes only the rows it seeded).
"""
from __future__ import annotations

from django.db import migrations

_OFFICER = {"kind": "role", "role": "officer"}
_CLAIMANT = {"kind": "context_user"}

_RULES = [
    # key, label, trigger_source, category, priority, audience, condition, title, body
    ("srp-submitted", "SRP claim submitted", "srp.submitted", "logistics", "normal", _OFFICER, {},
     "SRP claim submitted", "{pilot_name} submitted an SRP claim for {ship_name} ({isk} ISK)."),
    ("srp-approved", "SRP claim approved", "srp.approved", "logistics", "normal", _CLAIMANT, {},
     "SRP approved", "Your SRP claim was approved."),
    ("srp-denied", "SRP claim denied", "srp.denied", "logistics", "normal", _CLAIMANT, {},
     "SRP denied", "Your SRP claim was denied."),
    ("srp-paid", "SRP claim paid", "srp.paid", "logistics", "normal", _CLAIMANT, {},
     "SRP paid", "Your SRP claim was paid ({isk} ISK)."),
    ("logistics-new", "New courier contract", "logistics.new", "logistics", "normal", _OFFICER, {},
     "New courier contract", "A new courier contract is up: {destination_system}."),
    ("store-new", "New store order", "store.new", "system", "normal", _OFFICER, {},
     "New store order", "New corp store order: {ship_name}."),
    ("structure-fuel-low", "Structure low on fuel", "structure.fuel_low", "structure_timer", "high",
     _OFFICER, {"days_of_fuel_lt": 3},
     "Structure low on fuel", "{structure_name} has about {days_of_fuel} days of fuel left."),
    ("moon-fracture-ready", "Moon fracture approaching", "moon.fracture_ready", "moon_extraction",
     "normal", _OFFICER, {"hours_to_fracture_lt": 48},
     "Moon fracture approaching", "{moon_name} fractures in about {hours_to_fracture}h."),
    ("industry-job-complete", "Industry job completing", "industry.job_complete", "industry_job",
     "low", _OFFICER, {"minutes_to_complete_lt": 30},
     "Industry job completing", "{industry_job_name} completes in about {minutes_to_complete} min."),
]


def seed(apps, schema_editor):
    AutomationRule = apps.get_model("pingboard", "AutomationRule")
    for key, label, trigger, category, priority, audience, condition, title, body in _RULES:
        AutomationRule.objects.get_or_create(
            key=key,
            defaults={
                "label": label, "trigger_source": trigger, "category": category,
                "priority": priority, "audience": audience, "condition": condition,
                "title": title, "body": body, "enabled": False,
            },
        )


def unseed(apps, schema_editor):
    AutomationRule = apps.get_model("pingboard", "AutomationRule")
    AutomationRule.objects.filter(key__in=[r[0] for r in _RULES]).delete()


class Migration(migrations.Migration):
    dependencies = [("pingboard", "0007_alert_automation_rule")]
    operations = [migrations.RunPython(seed, unseed)]
