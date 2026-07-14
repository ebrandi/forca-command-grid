"""Existing-user migration for Linked Pilots (LP-4): give today's directors their seat back.

``is_corp_director`` is new and defaults to False, but the active-pilot authority ceiling reads
it: a director whose pilot is not flagged is capped at officer. The flag is otherwise only set
by the in-game Director ESI reconcile, which runs on a **six-hourly** beat — so without this
migration every director in the corporation would silently lose Director access at deploy and
get it back at some point in the next six hours. That is an outage, not a rollout.

The backfill starts from exactly today's semantics. Today a Director role grant is account-wide:
*every* pilot the director owns already wields Director authority. So we flag every corp-member
pilot of every user who currently holds a non-expired Director grant. Authority on the day of
the deploy is therefore unchanged — nobody gains anything, nobody loses anything — and the
six-hourly reconcile then narrows each account to the pilots that genuinely hold the in-game
role, which is the whole point of the feature.

Idempotent and safe to re-run: it is a conditional UPDATE, not an insert.
"""
from __future__ import annotations

from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def flag_director_seats(apps, schema_editor):
    EveCharacter = apps.get_model("sso", "EveCharacter")
    RoleAssignment = apps.get_model("identity", "RoleAssignment")

    now = timezone.now()
    director_user_ids = (
        RoleAssignment.objects.filter(role__key="director")
        # An expired grant confers nothing today (see identity.User.max_role_rank, which skips
        # them), so it must not confer a Director seat here either.
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .values_list("user_id", flat=True)
    )
    EveCharacter.objects.filter(
        user_id__in=list(director_user_ids), is_corp_member=True, is_corp_director=False
    ).update(is_corp_director=True)


def clear_director_seats(apps, schema_editor):
    """Reverse: drop the flag everywhere.

    Correct because the column did not exist before this feature — nothing else writes it, so
    there is no pre-existing state to preserve. The forward migration reconstructs it from the
    role grants, so re-applying restores the same result.
    """
    EveCharacter = apps.get_model("sso", "EveCharacter")
    EveCharacter.objects.filter(is_corp_director=True).update(is_corp_director=False)


class Migration(migrations.Migration):

    dependencies = [
        ("sso", "0005_evecharacter_display_order_and_more"),
        # The backfill reads identity.RoleAssignment.role.key and .expires_at.
        ("identity", "0004_user_language"),
    ]

    operations = [
        migrations.RunPython(flag_director_seats, clear_director_seats),
    ]
