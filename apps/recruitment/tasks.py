"""Celery tasks: refresh a candidate's public evidence off the request path."""
from __future__ import annotations

from celery import shared_task


@shared_task(name="recruitment.refresh_evidence")
def refresh_candidate_evidence(candidate_id: int) -> int:
    from .models import Candidate
    from .services import refresh_evidence

    candidate = Candidate.objects.filter(pk=candidate_id).first()
    if candidate is None:
        return 0
    return refresh_evidence(candidate)
