"""Fix + retarget the seeded ``logistics.new`` automation rule (roadmap 0.11).

The rule shipped (0008) aimed at officers with a body that read
``{destination_system}`` — a context key the caller populated from a non-existent
``end_system_name`` attribute, so the destination always rendered empty. Retarget it
to the hauler pool (members, who actually claim contracts) and enrich the body to use
the origin/destination/reward/jumps the caller now supplies.

Runs on fresh installs (right after 0008) and existing ones alike. Only corrects the
**untouched** seed — never clobbers a leadership customization — and is reversible.
"""
from __future__ import annotations

from django.db import migrations

_KEY = "logistics-new"
_OLD_AUDIENCE = {"kind": "role", "role": "officer"}
_NEW_AUDIENCE = {"kind": "role", "role": "member"}
_OLD_BODY = "A new courier contract is up: {destination_system}."
_NEW_BODY = (
    "A new courier contract is up: {origin_system} → {destination_system} "
    "for {reward} ISK ({jumps} jumps)."
)


def fix_rule(apps, schema_editor):
    AutomationRule = apps.get_model("pingboard", "AutomationRule")
    rule = AutomationRule.objects.filter(key=_KEY).first()
    if rule and rule.audience == _OLD_AUDIENCE and rule.body == _OLD_BODY:
        rule.audience = _NEW_AUDIENCE
        rule.body = _NEW_BODY
        rule.save()


def revert_rule(apps, schema_editor):
    AutomationRule = apps.get_model("pingboard", "AutomationRule")
    rule = AutomationRule.objects.filter(key=_KEY).first()
    if rule and rule.audience == _NEW_AUDIENCE and rule.body == _NEW_BODY:
        rule.audience = _OLD_AUDIENCE
        rule.body = _OLD_BODY
        rule.save()


class Migration(migrations.Migration):
    dependencies = [("pingboard", "0009_pilotcontactchannel_verify_code_expires_at")]
    operations = [migrations.RunPython(fix_rule, revert_rule)]
