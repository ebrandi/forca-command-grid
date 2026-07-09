from django.apps import AppConfig


class ReadinessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.readiness"

    def ready(self) -> None:
        # Importing ``dimensions`` self-registers every provider in the engine
        # registry at startup (doc 05 §2.2); providers import domain models only
        # inside ``compute()``, so this is safe during app loading. ``signals``
        # connects the task-done → finding-acknowledged hook (doc 12 §7.2).
        from . import dimensions, signals  # noqa: F401

        signals.connect()

