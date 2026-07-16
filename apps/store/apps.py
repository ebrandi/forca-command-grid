from django.apps import AppConfig


class StoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.store"

    def ready(self) -> None:
        from . import signals  # noqa: F401 — connect supply-completion receivers
