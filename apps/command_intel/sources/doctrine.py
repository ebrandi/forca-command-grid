"""Doctrine intelligence source (design doc 04 §2, category "doctrine").

Per active doctrine: how many corp pilots can field it
(``doctrines.services.doctrine_coverage``), leadership's primary flag + minimum
crew (``readiness.DoctrineReadinessConfig``), and hulls staged in corp stock
(``doctrines.supply.corp_on_hand`` over the doctrine's hull type-ids); plus the
top hull-stock shortfalls across all doctrines (``doctrines.supply.corp_priority_list``,
filtered to hulls). Aggregate counts only (doc 04 §5).
"""
from __future__ import annotations

from ..engine.base import OK, PARTIAL, UNKNOWN, SnapshotContext, SourceSlice
from ..engine.registry import register_source
from ._util import isk, now_iso

_TOP_SHORTFALLS = 10


class DoctrineSource:
    key = "doctrine"
    label = "Doctrine Coverage"
    category = "doctrine"
    default_enabled = True

    def collect(self, ctx: SnapshotContext) -> SourceSlice:
        from django.utils.text import slugify

        from apps.doctrines.models import Doctrine
        from apps.doctrines.services import doctrine_coverage
        from apps.doctrines.supply import corp_on_hand, corp_priority_list

        doctrines = list(
            Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
            .select_related("readiness_config")
            .prefetch_related("fits")
            .order_by("-priority", "name")
        )
        if not doctrines:
            return SourceSlice(
                key=self.key, version=1, facts={}, as_of=now_iso(),
                coverage_pct=0.0, status=UNKNOWN,
                notes=("no active doctrines configured",),
            )

        characters = ctx.characters

        # Every hull type-id across every doctrine → on-hand counts in a single read.
        per_doctrine_hulls: dict[int, set[int]] = {}
        hull_ids: set[int] = set()
        for doctrine in doctrines:
            ids = {f.ship_type_id for f in doctrine.fits.all() if f.ship_type_id}
            per_doctrine_hulls[doctrine.id] = ids
            hull_ids |= ids
        on_hand = corp_on_hand(hull_ids) if hull_ids else {}

        rows = []
        for doctrine in doctrines:
            counts = doctrine_coverage(doctrine, characters)
            cfg = getattr(doctrine, "readiness_config", None)
            rows.append({
                "name": doctrine.name,
                "slug": slugify(doctrine.name),
                "primary": bool(cfg.is_primary) if cfg else False,
                "flyable": counts["optimal"] + counts["viable"],
                "viable": counts["viable"],
                "not_ready": counts["not_ready"],
                "hulls_in_stock": sum(
                    int(on_hand.get(tid, 0)) for tid in per_doctrine_hulls[doctrine.id]
                ),
                "min_pilots": cfg.min_pilots if cfg else None,
            })

        shortfalls = [
            {"type": r["type_id"], "name": r["name"],
             "need": r["need"], "buy_isk": isk(r.get("cost"))}
            for r in corp_priority_list()
            if r["type_id"] in hull_ids
        ][:_TOP_SHORTFALLS]

        facts = {"doctrines": rows, "hull_shortfalls_top": shortfalls}
        if characters:
            return SourceSlice(
                key=self.key, version=1, facts=facts, as_of=now_iso(),
                coverage_pct=100.0, status=OK,
            )
        return SourceSlice(
            key=self.key, version=1, facts=facts, as_of=now_iso(),
            coverage_pct=None, status=PARTIAL,
            notes=("no corp members to measure coverage against",),
        )


register_source(DoctrineSource())
