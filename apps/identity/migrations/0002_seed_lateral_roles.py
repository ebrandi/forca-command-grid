"""Seed the least-privilege lateral roles (4.16): recruiter + FC, each granting one
capability permission. Idempotent (get_or_create); reversible drops the seeded rows only."""
from django.db import migrations

_PERMS = [
    ("recruitment.manage", "Manage the recruitment pipeline"),
    ("fleet.manage", "Create and run fleet operations"),
]
# role key -> (label, rank, [permission keys]). Rank 0: a lateral role adds a capability,
# never authority elsewhere (core.rbac keeps these keys out of ROLE_RANK).
_ROLES = [
    ("recruiter", "Recruiter", 0, ["recruitment.manage"]),
    ("fc", "Fleet Commander", 0, ["fleet.manage"]),
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
    Role.objects.filter(key__in=[r[0] for r in _ROLES]).delete()
    Permission.objects.filter(key__in=[p[0] for p in _PERMS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
