"""Per-recipient notification & email localisation (D14).

Additive, metadata-only fields (constant defaults → no table rewrite, no backfill):

* ``Alert.template_key`` + ``Alert.context`` — retain the message identity and the
  interpolation context ``emit_alert`` used, so the dispatcher and the in-app viewer can
  re-render title/body per recipient locale instead of fanning the single frozen
  ``Alert.body``. Existing rows read ``""``/``{}`` and fall back to the frozen body.
* ``AlertRecipient.language`` — the locale a per-pilot leg (in-app row / EVE-mail) was
  rendered in (debug/observability).
* ``AlertDelivery.language`` — the locale a shared/broadcast leg was rendered in.

Hand-written (the container runs as a different uid; ``makemessages``/``makemigrations``
would create root-owned files). Kept additive so existing rows stay valid.
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pingboard", "0013_alter_alert_category_alter_alerttemplate_category_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="alert",
            name="template_key",
            field=models.CharField(blank=True, default="", max_length=60),
        ),
        migrations.AddField(
            model_name="alert",
            name="context",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="alertrecipient",
            name="language",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="alertdelivery",
            name="language",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
    ]
