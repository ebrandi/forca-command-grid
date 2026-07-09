"""IND-1 (roadmap 3.3) — the Plan → Job bridge.

Turns a plan's buildable lines into claimable ERP ``BuildJob``s and links each job back to
its ``IndustryProjectItem`` so delivery flows to the plan's status + corp stock + builder
credit — one auditable demand → plan → job → stock → credit spine.
"""
from __future__ import annotations

from django.db import transaction

from .models import IndustryProject, IndustryProjectItem


@transaction.atomic
def push_project_to_jobs(project: IndustryProject, user) -> int:
    """Create a BuildJob for each BUILD line that doesn't already have an open/delivered job.

    Idempotent: re-pushing a plan won't duplicate a job for a line that still has one open
    (or already delivered). All-or-nothing (atomic). Returns the number of jobs created.
    """
    from apps.erp.models import BuildJob
    from apps.erp.services import recheck_block

    # A non-corp plan's name is not surfaced on the corp-visible job board.
    note = (
        f"From plan: {project.name}"[:200]
        if project.visibility == IndustryProject.Visibility.CORP
        else "From a leadership plan"
    )
    created = 0
    for item in project.items.filter(build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD):
        # An item with any non-cancelled job (open or delivered) is already on the board.
        if BuildJob.objects.filter(source_item=item).exclude(
            status=BuildJob.Status.CANCELLED
        ).exists():
            continue
        job = BuildJob.objects.create(
            output_type_id=item.type_id,
            quantity=max(1, int(item.quantity or 1)),
            created_by=user,
            source_item=item,
            note=note,
        )
        recheck_block(job)  # flag BLOCKED immediately if corp stock can't cover the inputs
        created += 1

    if created and project.status == IndustryProject.Status.DRAFT:
        project.status = IndustryProject.Status.ACTIVE
        project.save(update_fields=["status", "updated_at"])
    return created
