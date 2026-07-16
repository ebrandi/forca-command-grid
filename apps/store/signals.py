"""Signal receivers wiring production completion back to Shipyard supply needs.

An ERP build job delivering, or an Industry Project completing, does NOT touch
customer orders or fitted-ship stock (built hulls are not assembled doctrine
packages). It flags the linked supply need and tells the officers to assemble
and receipt the ships — the receipt is what allocates stock to waiting
backorders. Receivers are deliberately cheap: one indexed lookup deciding
whether anything is linked at all.
"""
from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="erp.BuildJob", dispatch_uid="store_supply_buildjob_done")
def _build_job_completed(sender, instance, **kwargs):
    from apps.erp.models import BuildJob

    if instance.status != BuildJob.Status.DELIVERED:
        return
    from .supply import on_vehicle_completed

    on_vehicle_completed(build_job=instance)


@receiver(post_save, sender="industry.IndustryProject", dispatch_uid="store_supply_project_done")
def _industry_project_completed(sender, instance, **kwargs):
    from apps.industry.models import IndustryProject

    if instance.status != IndustryProject.Status.DONE:
        return
    from .supply import on_vehicle_completed

    on_vehicle_completed(industry_project=instance)
