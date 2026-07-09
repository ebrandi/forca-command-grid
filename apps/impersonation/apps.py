from __future__ import annotations

from django.apps import AppConfig


class ImpersonationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.impersonation"
    label = "impersonation"
    verbose_name = "Director view-as (impersonation)"

    def ready(self) -> None:  # noqa: D401 - Django hook
        # Close any open impersonation when the director logs out, so a "log out
        # while viewing as a pilot" never leaves a session row wedged open.
        from . import signals  # noqa: F401
