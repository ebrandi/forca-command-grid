# Seam B: the persisted Command Intelligence prose gains a scaffold key + params alongside each
# English column, so a row written by a locale-less Celery worker (the report/battle/pilot jobs) can
# be re-rendered in the READER's locale (see ``apps/command_intel/messages.py``).
#
# Additive and metadata-only: no data migration, nothing is backfilled. A legacy row simply has no
# key and renders its stored English — never blank. An LLM-authored sentence likewise carries no key
# and renders verbatim, by contract.
#
# ``db_default`` on every new column is load-bearing. Without it Django emits
# ``ADD COLUMN … DEFAULT x NOT NULL`` and then immediately ``ALTER COLUMN … DROP DEFAULT``, which
# leaves a NOT NULL column with NO database-level default: an INSERT issued by the *old* code during
# a rollback then dies with a not-null violation, while reads keep working — so it looks healthy and
# breaks silently. This exact trap bit the production i18n deploy. Verified with ``sqlmigrate``:
# there is no DROP DEFAULT on any column below.
#
# NB ``Value({}, JSONField())`` — not ``Value("{}", …)``, which Django would encode as the JSON
# *string* ``"{}"`` and hand an old-code INSERT a ``str`` where every reader expects a dict.
from django.db import migrations, models


def _key():
    return models.CharField(blank=True, db_default="", default="", max_length=64)


def _params():
    return models.JSONField(
        blank=True,
        db_default=models.Value({}, models.JSONField()),
        default=dict,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("command_intel", "0007_savedsimscenario"),
    ]

    operations = [
        # OperationalConstraint.label / .detail — computed by the constraint providers.
        migrations.AddField(
            model_name="operationalconstraint", name="label_key", field=_key(),
        ),
        migrations.AddField(
            model_name="operationalconstraint", name="label_params", field=_params(),
        ),
        migrations.AddField(
            model_name="operationalconstraint", name="detail_key", field=_key(),
        ),
        migrations.AddField(
            model_name="operationalconstraint", name="detail_params", field=_params(),
        ),
        # IntelligenceReport.title / .summary / .body — the staff briefing.
        migrations.AddField(
            model_name="intelligencereport", name="title_key", field=_key(),
        ),
        migrations.AddField(
            model_name="intelligencereport", name="title_params", field=_params(),
        ),
        migrations.AddField(
            model_name="intelligencereport", name="summary_key", field=_key(),
        ),
        migrations.AddField(
            model_name="intelligencereport", name="summary_params", field=_params(),
        ),
        migrations.AddField(
            model_name="intelligencereport", name="body_key", field=_key(),
        ),
        migrations.AddField(
            model_name="intelligencereport", name="body_params", field=_params(),
        ),
        # CourseOfAction.objective / .reasoning / .risk_if_ignored — the decision payload.
        migrations.AddField(
            model_name="courseofaction", name="objective_key", field=_key(),
        ),
        migrations.AddField(
            model_name="courseofaction", name="objective_params", field=_params(),
        ),
        migrations.AddField(
            model_name="courseofaction", name="reasoning_key", field=_key(),
        ),
        migrations.AddField(
            model_name="courseofaction", name="reasoning_params", field=_params(),
        ),
        migrations.AddField(
            model_name="courseofaction", name="risk_if_ignored_key", field=_key(),
        ),
        migrations.AddField(
            model_name="courseofaction", name="risk_if_ignored_params", field=_params(),
        ),
        # PilotDirective.title / .detail — the member's quest log.
        migrations.AddField(
            model_name="pilotdirective", name="title_key", field=_key(),
        ),
        migrations.AddField(
            model_name="pilotdirective", name="title_params", field=_params(),
        ),
        migrations.AddField(
            model_name="pilotdirective", name="detail_key", field=_key(),
        ),
        migrations.AddField(
            model_name="pilotdirective", name="detail_params", field=_params(),
        ),
        # BattleAnalysis.title / .body — the after-action review.
        migrations.AddField(
            model_name="battleanalysis", name="title_key", field=_key(),
        ),
        migrations.AddField(
            model_name="battleanalysis", name="title_params", field=_params(),
        ),
        migrations.AddField(
            model_name="battleanalysis", name="body_key", field=_key(),
        ),
        migrations.AddField(
            model_name="battleanalysis", name="body_params", field=_params(),
        ),
    ]
