"""Readiness intelligence source (design doc 04 §2, category "strategic").

Turns the corp-wide readiness picture into one typed snapshot slice: the overall
index and per-dimension scores (``readiness.services.compute_readiness``), a cheap
30-day per-dimension trend from snapshot history, and the top owned findings
(open ``readiness.ReadinessFinding`` rows). Aggregates only — no pilot names cross
the boundary (doc 04 §5).
"""
from __future__ import annotations

from ..engine.base import OK, PARTIAL, UNKNOWN, SnapshotContext, SourceSlice
from ..engine.registry import register_source
from ._util import now_iso, pct

_TOP_FINDINGS = 8


class ReadinessSource:
    key = "readiness"
    label = "Corporation Readiness"
    category = "strategic"
    default_enabled = True

    def collect(self, ctx: SnapshotContext) -> SourceSlice:
        from apps.readiness.services import compute_readiness

        data = compute_readiness(use_cache=True)
        index = data.get("index")
        dim_scores = data.get("dimensions") or {}
        if index is None and not dim_scores:
            return SourceSlice(
                key=self.key, version=1, facts={}, as_of=now_iso(),
                coverage_pct=0.0, status=UNKNOWN,
                notes=("readiness index not yet computed (no doctrines/members)",),
            )

        trends = self._trend_30d(dim_scores)
        dimensions = [
            {"key": k, "score": v, "coverage_pct": None, "trend_30d": trends.get(k)}
            for k, v in sorted(dim_scores.items())
        ]

        coverage = data.get("coverage") or {}
        sample = coverage.get("characters") or 0
        known = coverage.get("known") or 0
        coverage_pct = pct(100.0 * known / sample) if sample else None

        facts = {
            "overall_index": int(index) if index is not None else None,
            "dimensions": dimensions,
            "top_findings": self._top_findings(),
        }
        status = OK if (coverage_pct is not None and coverage_pct >= 100) else PARTIAL
        return SourceSlice(
            key=self.key, version=1, facts=facts, as_of=now_iso(),
            coverage_pct=coverage_pct, status=status,
        )

    def _top_findings(self) -> list[dict]:
        from apps.readiness.models import ReadinessFinding

        rows = ReadinessFinding.objects.filter(
            status=ReadinessFinding.Status.OPEN
        ).order_by("-weight", "-last_seen")[:_TOP_FINDINGS]
        return [
            {"dimension": f.dimension_key, "severity": f.severity,
             "title": f.title, "age_days": f.age_days}
            for f in rows
        ]

    def _trend_30d(self, current: dict) -> dict:
        """Per-dimension score delta vs the nearest readiness snapshot ~30 days old."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.readiness.models import ReadinessSnapshot

        since = timezone.now() - timedelta(days=37)
        past = (
            ReadinessSnapshot.objects.filter(created_at__gte=since)
            .order_by("created_at")
            .values_list("dimensions", flat=True)
            .first()
        ) or {}
        out: dict = {}
        for key, now_v in current.items():
            old = past.get(key)
            if isinstance(now_v, int | float) and isinstance(old, int | float):
                out[key] = round(now_v - old)
        return out


register_source(ReadinessSource())
