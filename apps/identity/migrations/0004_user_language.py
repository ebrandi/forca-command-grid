from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the account-level UI language preference (docs/i18n/design/06-preference-data-model.md).

    Additive AddField with a blank default, so every existing user backfills
    deterministically to "" (= not explicitly chosen → the locale resolver may use
    the browser's Accept-Language before falling back to English). No table rewrite;
    fully reversible.
    """

    dependencies = [
        ("identity", "0003_role_change_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="language",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Preferred UI language (a settings.LANGUAGES code, e.g. 'pt-br'); "
                "blank = auto-detect from the browser, then English.",
                max_length=16,
            ),
        ),
    ]
