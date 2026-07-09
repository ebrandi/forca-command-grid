from django.apps import AppConfig


class CommandIntelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.command_intel"
    verbose_name = "Command Intelligence"

    def ready(self) -> None:
        # Importing ``sources`` and ``constraints`` self-registers every provider in
        # the engine registries at startup (design docs 04 §1, 05 §2); providers
        # import domain/service models only inside their collect()/compute(), so this
        # is safe during app loading. ``signals`` wires the task-done → COA-completion
        # loop (doc 07 §6).
        from . import constraints, signals, sources  # noqa: F401

        signals.connect()
