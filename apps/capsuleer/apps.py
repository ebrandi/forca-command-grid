from django.apps import AppConfig


class CapsuleerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.capsuleer"
    label = "capsuleer"
    verbose_name = "Capsuleer Path"

    def ready(self) -> None:
        from . import signals

        signals.connect()
