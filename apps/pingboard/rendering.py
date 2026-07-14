"""Sandboxed template rendering — prevents injection / traversal into objects.

Templates use ``{variable}`` placeholders over a **flat, pre-stringified** context.
A custom formatter forbids attribute/index access (``{a.b}``, ``{a[0]}``) and
positional fields, so a template can only ever reference declared scalar variables —
it can never walk an object graph into a secret. Literal braces escape as ``{{``/``}}``.
"""
from __future__ import annotations

import string

# Lazy, not eager: these errors are raised inside `translation.override(broadcast_locale())`
# (services.emit_alert), but they surface to the composing officer through `str(exc)` in
# pingboard.views AFTER that override has exited. An eager gettext would resolve them in the
# broadcast locale — English — and show English errors on an otherwise translated page.
from django.utils.translation import gettext_lazy as _


class TemplateError(ValueError):
    """Invalid template body or missing required variable."""


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        # Disallow anything but a bare name (no ``.attr`` / ``[index]`` traversal).
        if any(ch in field_name for ch in ".[]"):
            raise TemplateError(
                _("disallowed field expression: %(field)r") % {"field": field_name}
            )
        return super().get_field(field_name, args, kwargs)

    def get_value(self, key, args, kwargs):
        if isinstance(key, int):
            raise TemplateError(_("positional fields are not allowed in templates"))
        return kwargs.get(key, "")  # unknown variable renders empty (validated separately)


_FORMATTER = _SafeFormatter()


def _flatten(context: dict | None) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in (context or {}).items()}


def render(text: str, context: dict | None = None) -> str:
    """Render ``text`` against ``context``. Raises :class:`TemplateError` on a bad body."""
    if not text:
        return ""
    try:
        return _FORMATTER.vformat(text, (), _flatten(context))
    except TemplateError:
        raise
    except (ValueError, KeyError, IndexError) as exc:
        raise TemplateError(_("invalid template: %(error)s") % {"error": exc}) from exc


def missing_required(required_vars, context: dict | None) -> list[str]:
    """Required variables that are absent or blank in ``context``."""
    ctx = context or {}
    return [v for v in (required_vars or []) if not str(ctx.get(v, "")).strip()]


# The template-variable catalogue (documentation + composer preview helper).
VARIABLE_CATALOGUE = [
    "pilot_name", "corp_name", "operation_name", "fleet_type", "fleet_commander",
    "formup_system", "destination_system", "start_time", "doctrine_name", "required_ships",
    "moon_name", "structure_name", "industry_job_name", "alert_priority", "alert_category",
    "calendar_event_title", "calendar_event_start", "link",
]
