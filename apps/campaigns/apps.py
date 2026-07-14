from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CampaignsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.campaigns"
    label = "campaigns"
    verbose_name = _("Campaign Command")

    def ready(self) -> None:
        from . import metrics, signals  # noqa: F401 — importing metrics registers the sources

        signals.connect()
