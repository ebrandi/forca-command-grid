"""Decide whether rolling the CODE back — while leaving the SCHEMA where it is — is safe.

The tempting rule is "the migrations were additive, so the old code will tolerate them".
That rule is wrong, and believing it has already broken this installation once.

``AddField(default=...)`` does not leave a default behind in the database. Django enforces
defaults in Python, so it emits::

    ALTER TABLE t ADD COLUMN c varchar(64) DEFAULT 'x' NOT NULL;
    ALTER TABLE t ALTER COLUMN c DROP DEFAULT;

The column ends up **NOT NULL with no database default**. Roll the code back and every
SELECT still works, ``/healthz`` still passes, the site looks fine — while every INSERT from
the older code (which knows nothing about column ``c``, and so does not supply it) dies on a
not-null violation. The breakage is silent, delayed, and lands on whatever writes first:
new-pilot registration, alert emission, a Celery task at 3am.

``db_default=`` (Django 5.0+) is the fix: the default lives in the database, so an INSERT
that omits the column is filled in by PostgreSQL and old code keeps writing.

This command answers the question empirically. It does not trust the migration files — it
asks the database what the columns actually look like right now, which also means a default
restored by hand (``ALTER TABLE ... ALTER COLUMN ... SET DEFAULT``) is correctly seen as
safe.

Usage (``scripts/rollback.sh`` calls this for you)::

    python manage.py rollback_safety --migration campaigns.0006_foo --migration erp.0007_bar

Exit status: 0 = a code-only rollback is safe, 1 = it is not.
"""
from __future__ import annotations

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader

# Operations that change or remove something the older code still expects to find.
# Additive-only is a precondition for a code-only rollback; these break it outright.
DESTRUCTIVE_OPS = {
    "RemoveField",
    "RenameField",
    "AlterField",
    "DeleteModel",
    "RenameModel",
    "AlterModelTable",
}


class Command(BaseCommand):
    help = "Report whether a code-only rollback past the given migrations is safe."

    def add_arguments(self, parser):
        parser.add_argument(
            "--migration",
            action="append",
            default=[],
            metavar="app_label.migration_name",
            help="A migration that exists in the CURRENT code but not in the rollback target. Repeatable.",
        )

    def handle(self, *args, **options):
        targets = options["migration"]
        if not targets:
            self.stdout.write("No migrations are being left behind — a code-only rollback is safe.")
            return

        loader = MigrationLoader(None, ignore_no_migrations=True)
        added_columns: list[tuple[str, str, str]] = []  # (migration, table, column)
        destructive: list[tuple[str, str]] = []  # (migration, operation)

        for ident in targets:
            app_label, _, name = ident.partition(".")
            if not app_label or not name:
                raise CommandError(f"Expected 'app_label.migration_name', got: {ident!r}")
            migration = loader.disk_migrations.get((app_label, name))
            if migration is None:
                # The migration is applied in the database but its file is not on disk. We
                # cannot inspect what it did, so we cannot certify the rollback.
                raise CommandError(
                    f"Migration {ident} is not on disk — cannot verify what it changed. "
                    "Refusing to certify this rollback."
                )

            for op in migration.operations:
                op_name = type(op).__name__
                if op_name in DESTRUCTIVE_OPS:
                    destructive.append((ident, f"{op_name} on {getattr(op, 'model_name', '?')}"))
                    continue
                if op_name != "AddField":
                    continue  # CreateModel/AddIndex/RunPython: an extra table or index is inert to old code
                try:
                    model = django_apps.get_model(app_label, op.model_name)
                    field = model._meta.get_field(op.name)
                    added_columns.append((ident, model._meta.db_table, field.column))
                except LookupError:
                    # Model or field has since been removed; the column may still exist, but we
                    # cannot resolve its name. Treat as unverifiable rather than silently safe.
                    destructive.append((ident, f"AddField {op.model_name}.{op.name} (model no longer resolvable)"))

        unsafe = self._unsafe_columns(added_columns)

        # ---- report -------------------------------------------------------------------
        self.stdout.write(
            f"Inspecting {len(targets)} migration(s) that the rollback target does not have; "
            f"{len(added_columns)} added column(s)."
        )

        if destructive:
            self.stdout.write(self.style.ERROR("\nNot additive — the old code cannot run against this schema:"))
            for ident, what in destructive:
                self.stdout.write(f"  {ident}: {what}")

        if unsafe:
            self.stdout.write(
                self.style.ERROR("\nNOT NULL with no database default — the old code's INSERTs will fail:")
            )
            for table, column in unsafe:
                self.stdout.write(f"  {table}.{column}")
            self.stdout.write(
                "\nEach of these columns is required by the database but unknown to the code you are "
                "rolling back to, so every INSERT into these tables would raise a not-null violation. "
                "Reads would keep working, which is what makes it so easy to miss."
            )

        if destructive or unsafe:
            self.stdout.write(self.style.WARNING("\nYour options, best first:"))
            if unsafe and not destructive:
                self.stdout.write(
                    "  1. Give the columns a database default, which makes the old code writable again:\n"
                    + "".join(
                        f"       ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT '<the value the new code uses>';\n"
                        for t, c in unsafe
                    )
                    + "     (metadata-only, a few milliseconds each; verify with a rolled-back INSERT.)"
                )
            self.stdout.write(
                "  2. Roll the DATABASE back too: scripts/rollback.sh <ref> --restore <dump.sql.gz>\n"
                "     Fully consistent, but discards every row written since that dump."
            )
            self.stdout.write(
                "  3. Reverse the migrations FIRST, using the CURRENT image (it is the only one that has\n"
                "     the migration files), then swap the code."
            )
            raise CommandError("A code-only rollback is NOT safe against this schema.")

        self.stdout.write(
            self.style.SUCCESS(
                "\nSafe: every column these migrations added is either nullable or still carries a "
                "database default, so the older code can continue to INSERT."
            )
        )

    def _unsafe_columns(self, added_columns) -> list[tuple[str, str]]:
        """Of the added columns, those the DB requires but the old code will never supply."""
        if not added_columns:
            return []
        pairs = sorted({(table, column) for _, table, column in added_columns})
        tables = [t for t, _ in pairs]
        columns = [c for _, c in pairs]
        with connection.cursor() as cur:
            # Match on the (table, column) pairs we actually added. Unnesting two parallel
            # arrays keeps this a single parameterised statement — a row-values IN list is
            # not adaptable by the driver.
            cur.execute(
                """
                SELECT c.table_name, c.column_name
                FROM information_schema.columns AS c
                JOIN unnest(%s::text[], %s::text[]) AS added(table_name, column_name)
                  ON c.table_name = added.table_name
                 AND c.column_name = added.column_name
                WHERE c.table_schema = current_schema()
                  AND c.is_nullable = 'NO'
                  AND c.column_default IS NULL
                """,
                [tables, columns],
            )
            return sorted(cur.fetchall())
