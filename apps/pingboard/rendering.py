"""Sandboxed template rendering — prevents injection / traversal into objects.

Templates use ``{variable}`` placeholders over a **flat, pre-stringified** context.
A custom formatter forbids attribute/index access (``{a.b}``, ``{a[0]}``) and
positional fields, so a template can only ever reference declared scalar variables —
it can never walk an object graph into a secret. Literal braces escape as ``{{``/``}}``.
"""
from __future__ import annotations

import re
import string

# Lazy, not eager: these errors are raised inside `translation.override(broadcast_locale())`
# (services.emit_alert), but they surface to the composing officer through `str(exc)` in
# pingboard.views AFTER that override has exited. An eager gettext would resolve them in the
# broadcast locale — English — and show English errors on an otherwise translated page.
from django.utils.translation import gettext_lazy as _


class TemplateError(ValueError):
    """Invalid template body or missing required variable."""


_MAX_PAD = 200          # no alert body legitimately pads a slot wider than this
_WIDTH = re.compile(r"\d+")


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

    def format_field(self, value, format_spec):
        # ``get_field`` guards the field NAME; the format spec is parsed separately and can
        # still pad. "{pilot_name:>50000000}" turns a 3-character value into a 50 MB string —
        # and since a body is now re-rendered once per recipient locale, that cost is paid N
        # times, not once. Authoring a body is officer-gated, so this is a guard rail rather
        # than a boundary, but a padding width has no legitimate use here at all.
        if format_spec and _WIDTH.search(format_spec):
            width = max(int(m) for m in _WIDTH.findall(format_spec))
            if width > _MAX_PAD:
                raise TemplateError(
                    _("format width %(width)s exceeds the maximum of %(max)s")
                    % {"width": width, "max": _MAX_PAD}
                )
        return super().format_field(value, format_spec)


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


# The template-variable catalogue (documentation + composer preview helper) — ALSO the
# closed set of slot names a code message-scaffold (``messages.SCAFFOLDS``) may interpolate
# (doc 08 §11.1). Every entry is a *raw* value: the interpolated EVE/game/user datum is
# substituted verbatim and never passes through gettext.
VARIABLE_CATALOGUE = [
    # Fleet / operations
    "pilot_name", "corp_name", "operation_name", "fleet_type", "fleet_commander",
    "formup_system", "destination_system", "origin_system", "system_name", "route_name",
    "start_time",
    "doctrine_name", "required_ships", "required_count",
    # Structures / industry / moons
    "moon_name", "structure_name", "industry_job_name", "timer_type", "timer_side",
    "timer_time", "planet_type",
    # Alert metadata + deep links
    "alert_priority", "alert_category", "calendar_event_title", "calendar_event_start",
    "link", "opt_out_link", "event_label", "event_time",
    # Pilots / identity
    "character_name", "actor_name", "role_name", "mentor_name", "mentee_name",
    # Killboard
    "rank_name", "kill_count", "entity_type", "entity_name", "watchlist_name",
    # Campaigns / capsuleer
    "campaign_name", "objective_title", "milestone_title", "goal_title", "item_title",
    "assignment_label", "health_label", "review_month",
    # Store / raffle / logistics
    "ship_name", "status_label", "contest_name", "ticket_count", "prize_name", "prize_rank",
    # Shipyard availability (SHIP-1): backorder estimates, stock allocation, restock
    "eta_date", "quantity", "fit_name",
    # Generic scalars shared by many scaffolds
    "count", "minutes", "hours", "reason", "details", "scopes", "threat_count",
]
