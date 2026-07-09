from django.apps import AppConfig


class MentorshipConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mentorship"
    label = "mentorship"
    verbose_name = "Mentorship Program"

    def ready(self) -> None:
        # Importing ``validation`` self-registers every task validator in the
        # module-level registry at startup (mirrors the readiness engine). The
        # validators import domain models only inside their check functions, so
        # this is safe during app loading.
        from . import validation  # noqa: F401
