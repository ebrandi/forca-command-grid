"""Seed the ``campaign_lead`` lateral role (rank 0) granting ``campaign.manage``.

Copies the shape of ``apps/identity/migrations/0002_seed_lateral_roles.py``: idempotent
(get_or_create), reversible (drops the seeded role and the ``campaign.manage`` permission **only
when no other role still references it**). Officers/directors already hold the capability through
``core.rbac._PERM_RANK_BASELINE`` — this seed only enables *lateral* grants to a member who runs
campaigns without officer authority elsewhere. Uses ``apps.get_model`` so it operates on historical
models, never direct imports.
"""
from django.db import migrations

_PERMS = [
    ("campaign.manage", "Create and run strategic campaigns"),
]
# role key -> (label, rank, [permission keys]). Rank 0: a lateral role adds a capability, never
# authority elsewhere (core.rbac keeps these keys out of ROLE_RANK).
_ROLES = [
    ("campaign_lead", "Campaign Lead", 0, ["campaign.manage"]),
]


def seed(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Role = apps.get_model("identity", "Role")
    perms = {}
    for key, label in _PERMS:
        perms[key] = Permission.objects.get_or_create(key=key, defaults={"label": label})[0]
    for key, label, rank, perm_keys in _ROLES:
        role, _ = Role.objects.get_or_create(key=key, defaults={"label": label, "rank": rank})
        role.permissions.add(*[perms[k] for k in perm_keys])


def unseed(apps, schema_editor):
    Role = apps.get_model("identity", "Role")
    Permission = apps.get_model("identity", "Permission")
    seeded_role_keys = [r[0] for r in _ROLES]
    # Deleting the seeded roles drops their permission links via the through table; then delete a
    # seeded Permission row only when no OTHER role still references it — never cascade the
    # campaign.manage grant off a custom role that adopted it (#35).
    Role.objects.filter(key__in=seeded_role_keys).delete()
    for perm_key, _label in _PERMS:
        perm = Permission.objects.filter(key=perm_key).first()
        if perm is not None and not perm.roles.exists():
            perm.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0001_initial"),
        ("identity", "0002_seed_lateral_roles"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
