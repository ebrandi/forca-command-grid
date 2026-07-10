from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.campaigns"
    label = "campaigns"
    verbose_name = "Campaign Command"

    def ready(self) -> None:
        from . import metrics, signals  # noqa: F401 — importing metrics registers the sources

        signals.connect()
