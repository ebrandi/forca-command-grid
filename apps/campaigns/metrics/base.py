"""Metric-source framework: value object, source base, registry, fail-soft wrapper.

The auto-measurement half of Campaign Command (design doc 00 §6, doc 08 §2). A *source* turns
an objective's ``metric_params`` into a :class:`Measurement` by reading another app's service
(never writing it — the outward-only dependency rule, doc 02 §1). Sources self-register on import
(``apps.campaigns.metrics.__init__`` imports every module), so adding one is a pure registration
with no sweep-code edit — the ``apps.command_intel.engine.registry`` / ``apps.readiness`` idiom.

Two honesty rules carried from the readiness "honest score" pattern (``apps/readiness/engine/base``):

* a source that raises is isolated by :func:`measure_safely` — the objective keeps its last value
  and stale ``measured_at``, no sample is written, and the staleness surfaces in the UI. One broken
  source never aborts the sweep for the others (doc 08 §2.1 failure behaviour);
* the returned ``as_of`` is the backing data's own provenance instant where one exists (e.g.
  ``Max(as_of)`` over the snapshot rows read), and ``timezone.now()`` only for a live DB count that
  is inherently current — so the freshness chip never claims data is newer than it is.

``params_schema`` is a declarative field list the objective form renders and :func:`clean_params`
validates against; campaigns never trusts free-form ``metric_params`` (doc 04 §3, doc 12 §3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

logger = logging.getLogger("forca.campaigns")


@dataclass(frozen=True)
class Measurement:
    """One reading: the value, the honest ``as_of`` provenance instant, and a detail dict.

    ``detail`` carries caveats/breakdown (e.g. ``{"covered": False}`` for an uncovered stockpile,
    the DONE-is-manual note for operations) surfaced in the objective's measurement panel — data
    only, never markup.
    """

    value: Decimal
    as_of: datetime
    detail: dict = field(default_factory=dict)


class MetricSource:
    """Base for one auto-measured objective source.

    Subclasses set the class attributes and implement :meth:`measure`. ``data_class`` is a
    ``core.freshness`` threshold key driving the staleness chip; ``sensitive_default`` makes the
    objective form default ``is_sensitive`` on (finance/SRP figures, doc 04 §4.12/§4.13).
    """

    key: str = ""
    label: str = ""
    unit: str = ""
    data_class: str = "default"
    sensitive_default: bool = False
    params_schema: list[dict] = []

    def measure(self, params: dict) -> Measurement:
        """Return a :class:`Measurement` for ``params`` (validated user params merged with the
        service-injected campaign context from :func:`build_call_params`). Raising is allowed —
        the sweep wraps every call in :func:`measure_safely`."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Registry (register-on-import; ``get_source`` never looked up by the sweep)
# --------------------------------------------------------------------------- #
_SOURCES: dict[str, MetricSource] = {}


def register(source: MetricSource) -> MetricSource:
    """Register (or replace, by ``key``) a source. Idempotent — a re-import re-registers the same
    key rather than duplicating it."""
    _SOURCES[source.key] = source
    return source


def get_source(key: str) -> MetricSource | None:
    return _SOURCES.get(key) if key else None


def all_sources() -> list[MetricSource]:
    """Every registered source, ordered by label for the objective-form picker."""
    return sorted(_SOURCES.values(), key=lambda s: s.label.lower())


def unregister(key: str) -> None:
    """Test helper — drop a source registration."""
    _SOURCES.pop(key, None)


# --------------------------------------------------------------------------- #
#  Fail-soft measurement + call-param assembly
# --------------------------------------------------------------------------- #
def measure_safely(source: MetricSource, params: dict) -> Measurement | None:
    """Call ``source.measure(params)``, isolating any failure (doc 08 §2.1).

    On exception logs to ``forca.campaigns`` (key only — never the params, which can name a
    sensitive wallet division) and returns ``None``; the caller then leaves the objective's last
    value and stale ``measured_at`` untouched and writes no sample.
    """
    try:
        return source.measure(params)
    except Exception:  # noqa: BLE001 — one broken source must never abort the sweep
        logger.exception("campaigns.metric_source_failed key=%s", getattr(source, "key", "?"))
        return None


def build_call_params(objective) -> dict:
    """Merge an objective's stored user params with the campaign context the windowed sources read.

    Reserved ``_``-prefixed keys (``_since`` = campaign start, ``_now`` = measurement instant,
    ``_operation_ids`` = linked op ids, ``_campaign_id``) are injected for the call only and are
    never persisted back to ``metric_params`` — the schema-visible params stay clean.
    """
    from django.utils import timezone

    campaign = objective.campaign
    params = dict(objective.metric_params or {})
    params["_since"] = campaign.start_at
    params["_now"] = timezone.now()
    params["_campaign_id"] = campaign.pk
    params["_operation_ids"] = list(
        campaign.linked_operations.values_list("operation_id", flat=True)
    )
    return params


# --------------------------------------------------------------------------- #
#  Declarative params validation (form/service boundary, never the beat)
# --------------------------------------------------------------------------- #
def clean_params(source: MetricSource, raw: dict) -> dict:
    """Validate ``raw`` against ``source.params_schema`` → a clean ``metric_params`` dict.

    Raises :class:`ValidationError` on a missing required field, a non-numeric int, an empty int
    list, or a choice outside its option set — so a malformed source configuration is rejected at
    the objective form, never inside the refresh beat (doc 12 §3c). Unknown keys are dropped.
    """
    cleaned: dict = {}
    for field_spec in source.params_schema:
        name = field_spec["name"]
        kind = field_spec.get("kind", "str")
        required = bool(field_spec.get("required"))
        label = field_spec.get("label", name)
        value = raw.get(name)

        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ValidationError(
                    _("“%(label)s” is required for this metric source.") % {"label": label}
                )
            continue

        if kind == "int":
            cleaned[name] = _clean_int(value, label)
        elif kind == "ints":
            cleaned[name] = _clean_ints(value, label, required)
        elif kind == "choice":
            choices = {str(c[0]) for c in resolve_choices(field_spec)}
            if str(value) not in choices:
                raise ValidationError(
                    _("“%(label)s” is not one of the allowed options.") % {"label": label}
                )
            cleaned[name] = str(value)
        else:  # str
            cleaned[name] = str(value).strip()[:120]
    return cleaned


def resolve_choices(field_spec: dict) -> list:
    """Resolve a ``choice`` field's options — a static list, or a callable resolved lazily so a
    source can name another app's ``TextChoices`` without importing its models at package load."""
    spec = field_spec.get("choices")
    if callable(spec):
        try:
            return list(spec())
        except Exception:  # noqa: BLE001 — a picker with no options is degraded, never fatal
            return []
    return list(spec or [])


def _clean_int(value, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            _("“%(label)s” must be a whole number.") % {"label": label}
        ) from exc


def _clean_ints(value, label: str, required: bool) -> list[int]:
    if isinstance(value, list | tuple):
        parts = [str(v) for v in value]
    else:
        parts = [p for p in str(value).replace(" ", "").split(",") if p]
    out: list[int] = []
    for part in parts:
        try:
            out.append(int(part))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                _("“%(label)s” must be a comma-separated list of whole numbers.")
                % {"label": label}
            ) from exc
    if required and not out:
        raise ValidationError(
            _("“%(label)s” is required for this metric source.") % {"label": label}
        )
    return out


def _dec(value) -> Decimal:
    """Coerce a count/aggregate to ``Decimal`` for a :class:`Measurement` (``None`` → 0)."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)
