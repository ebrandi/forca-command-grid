"""One-click supply actions for a Tocha's Lab fit.

Turn a fit's corp-stock shortfall into a supply vehicle — a claimable corp task, an
Industry Center project, or a draft purchase order — each delegating to the canonical
FORCA authority (``apps.tasks`` / ``apps.industry`` / ``apps.procurement``). This module
only shapes a fit's shortfall into the payload each authority expects; it never
re-implements task creation, project BOM expansion, or PO drafting (see
handbooks/contributor-handbook/decision-log.md TL1/TL2).

The shortfall is always read from a *saved* revision through the same
``services.stock_coverage`` overlay the detail page shows, so a button can never act on
numbers the pilot did not see. Every action is audited and returns ``None`` when the fit
is already fully covered (the caller reports that honestly rather than minting an empty
task/project/PO).
"""
from __future__ import annotations

from django.db import transaction
from django.utils.translation import gettext as _

from core.audit import audit_log

from . import services
from .models import Fit, FitRevision

# Soft-link ref for the tasks factory's idempotency (one open task per fit).
RELATED_TYPE = "tochaslab_fit"


def fit_shortfall(ship_type_id: int, items: list[dict]) -> list[dict]:
    """Fit components corp stock cannot cover: ``[{type_id, name, short}]``.

    Reuses the read-only stock-coverage overlay (which reads
    ``apps.stockpile.availability.available``); never a second stock query path."""
    return services.stock_coverage(ship_type_id, items)["missing"]


def _shortfall_lines(shortfall: list[dict]) -> str:
    return ", ".join(f"{m['name']} ×{m['short']}" for m in shortfall)


@transaction.atomic
def create_shopping_task(fit: Fit, revision: FitRevision, actor):
    """A claimable corp *buy* task for the fit's missing components.

    Idempotent per fit: the tasks factory's ``related_type``/``related_id`` dedupe returns
    the existing open task rather than forking a duplicate on a repeat click."""
    from apps.tasks.models import Task
    from apps.tasks.services import create_task

    shortfall = fit_shortfall(revision.ship_type_id, revision.items)
    if not shortfall:
        return None
    task = create_task(
        task_type=Task.Type.BUY,
        title=_("Source components for %(fit)s") % {"fit": fit.name},
        description=_(
            "Tocha's Lab fit “%(fit)s” needs, beyond corp stock: %(lines)s."
        ) % {"fit": fit.name, "lines": _shortfall_lines(shortfall)},
        priority=5,
        related_type=RELATED_TYPE,
        related_id=fit.pk,
        created_by=actor,
    )
    audit_log(actor, "tochaslab.supply.task", target_type="fitting.Fit", target_id=fit.pk,
              metadata={"task": task.pk, "components": len(shortfall)})
    return task


@transaction.atomic
def create_industry_project(fit: Fit, revision: FitRevision, actor):
    """An Industry Center project to stock the fit's missing components.

    Each shortfall line becomes a project item; the plan's own BOM/shopping-list and
    build-vs-buy tooling take it from there (far better than a static material dump
    here). Created as a DRAFT so an officer reviews it before it goes active."""
    from apps.industry.models import IndustryProject, IndustryProjectItem

    shortfall = fit_shortfall(revision.ship_type_id, revision.items)
    if not shortfall:
        return None
    project = IndustryProject.objects.create(
        name=_("Stock components for %(fit)s") % {"fit": fit.name},
        description=_(
            "Missing-from-stock components for the Tocha's Lab fit “%(fit)s”."
        ) % {"fit": fit.name},
        objective_type=IndustryProject.Objective.STOCK,
        status=IndustryProject.Status.DRAFT,
        source=IndustryProject.Source.MANUAL,
        created_by=actor,
    )
    IndustryProjectItem.objects.bulk_create([
        IndustryProjectItem(
            project=project, type_id=int(m["type_id"]), product_name=m["name"][:200],
            quantity=max(1, int(m["short"])),
        )
        for m in shortfall
    ])
    audit_log(actor, "tochaslab.supply.project", target_type="fitting.Fit", target_id=fit.pk,
              metadata={"project": project.pk, "components": len(shortfall)})
    return project


@transaction.atomic
def create_purchase_order(fit: Fit, revision: FitRevision, actor, supplier, location=None):
    """A DRAFT purchase order to a supplier for the fit's missing components.

    Delegates to ``procurement.services.create_draft_po`` (MOQ rounding, catalogue lead
    time, the officer submit/approve lifecycle) — this only supplies the lines. Location
    falls back to the supplier's default when the caller passes none."""
    from apps.procurement.services import create_draft_po

    shortfall = fit_shortfall(revision.ship_type_id, revision.items)
    if not shortfall:
        return None
    po = create_draft_po(
        supplier=supplier,
        location=location or supplier.default_location,
        actor=actor,
        lines=[{"type_id": int(m["type_id"]), "quantity": max(1, int(m["short"]))}
               for m in shortfall],
    )
    audit_log(actor, "tochaslab.supply.po", target_type="fitting.Fit", target_id=fit.pk,
              metadata={"po": po.pk, "supplier": supplier.pk, "components": len(shortfall)})
    return po
