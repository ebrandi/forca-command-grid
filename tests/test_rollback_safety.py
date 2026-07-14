"""The rollback safety gate — `manage.py rollback_safety`.

This exists because "the migrations were additive, so a code-only rollback is safe" is a
lie that already cost this installation an outage. `AddField(default=...)` adds the column
NOT NULL and then DROPs the database default, because Django enforces defaults in Python.
Roll the code back and reads keep passing while every INSERT from the older code — which
never supplies the column it does not know about — dies on a not-null violation.

The command asks the *database* what the columns actually look like, rather than trusting
the migration file. That is deliberate: it means a default restored by hand
(`ALTER TABLE ... SET DEFAULT`) is correctly recognised as having made the rollback safe.

Both directions are pinned below, against real migrations in this repo. If one of these
ever flips, the schema genuinely changed and `scripts/rollback.sh` now behaves differently —
which is exactly what you want a test to tell you.
"""
from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command


@pytest.mark.django_db
def test_db_default_columns_are_reported_safe(capsys):
    """The i18n seam migrations use db_default=, so the old code can still INSERT."""
    call_command(
        "rollback_safety",
        "--migration", "campaigns.0006_campaign_source_key_milestone_source_key_and_more",
        "--migration", "readiness.0012_finding_alert_i18n_keys",
    )
    out = capsys.readouterr().out
    assert "Safe:" in out, out


@pytest.mark.django_db
def test_a_not_null_column_with_no_db_default_is_refused():
    """identity.0004 added `language` with default="" and no db_default — the classic trap.

    If this ever starts passing, someone gave the column a database default. Good — but then
    this test must be re-pointed at another example, not deleted: the gate itself still needs
    a case that proves it can say no.
    """
    with pytest.raises(CommandError, match="NOT safe"):
        call_command("rollback_safety", "--migration", "identity.0004_user_language")


@pytest.mark.django_db
def test_the_offending_column_is_named(capsys):
    """A refusal that does not tell you WHICH column is useless at 3am."""
    with pytest.raises(CommandError):
        call_command("rollback_safety", "--migration", "identity.0004_user_language")
    out = capsys.readouterr().out
    assert "identity_user.language" in out, out
    assert "ALTER TABLE identity_user ALTER COLUMN language SET DEFAULT" in out, out


@pytest.mark.django_db
def test_no_migrations_left_behind_is_trivially_safe(capsys):
    call_command("rollback_safety")
    assert "safe" in capsys.readouterr().out.lower()


@pytest.mark.django_db
def test_an_unknown_migration_is_refused_rather_than_assumed_safe():
    """A migration applied in the DB but missing from disk cannot be certified."""
    with pytest.raises(CommandError, match="not on disk"):
        call_command("rollback_safety", "--migration", "identity.0999_does_not_exist")
