from django.apps import AppConfig


class SupplyboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.supplyboard"

    def ready(self) -> None:
        # Import the providers module so the v1 built-ins self-register into REGISTRY.
        # Later phases (P4/P5/P6) register their own providers from THEIR AppConfig.ready().
        from . import providers  # noqa: F401
