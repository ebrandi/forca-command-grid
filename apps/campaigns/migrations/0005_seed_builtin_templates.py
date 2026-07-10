"""Seed the built-in campaign templates (doc 04 §13, brief §3).

Idempotent, reversible data migration: the forward step upserts every blueprint from
``apps.campaigns.templates_builtin`` by ``key`` (re-running is a no-op); the reverse step deletes
only the builtin rows (by their known keys), leaving any custom "save-as-template" rows untouched.
Mirrors the raffle ``seed_builtin_templates`` + identity ``0002_seed_lateral_roles`` pattern.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from apps.campaigns.templates_builtin import seed_builtin_templates

    CampaignTemplate = apps.get_model("campaigns", "CampaignTemplate")
    seed_builtin_templates(model=CampaignTemplate)


def unseed(apps, schema_editor):
    from apps.campaigns.templates_builtin import BUILTIN_KEYS

    CampaignTemplate = apps.get_model("campaigns", "CampaignTemplate")
    CampaignTemplate.objects.filter(key__in=BUILTIN_KEYS, is_builtin=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0004_objective_campaigns_o_metric__44712f_idx"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
