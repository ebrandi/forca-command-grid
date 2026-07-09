"""Combat intelligence source (design doc 04 §2, category "combat").

Turns the killboard's pre-computed combat rollups into one snapshot slice: the
all-time efficiency/danger headline (``killboard.analytics.summary``), the
last-30-day kill/loss/ISK window (the ``CombatMetric`` ``30d`` rollup), and the
recent doctrine loss patterns + repeat-loss pilot COUNT
(``killboard.analytics.loss_impact_summary``). Counts only — no pilot names cross
the boundary (doc 04 §5).
"""
from __future__ import annotations

from ..engine.base import OK, UNKNOWN, SnapshotContext, SourceSlice
from ..engine.registry import register_source
from ._util import isk, now_iso

_TOP_PATTERNS = 8


class CombatSource:
    key = "combat"
    label = "Combat Performance"
    category = "combat"
    default_enabled = True

    def collect(self, ctx: SnapshotContext) -> SourceSlice:
        from apps.killboard.analytics import loss_impact_summary, summary

        head = summary()
        last_30d = self._window_30d()
        if not any(
            (head.get("kills"), head.get("losses"),
             last_30d["kills"], last_30d["losses"])
        ):
            return SourceSlice(
                key=self.key, version=1, facts={}, as_of=now_iso(),
                coverage_pct=0.0, status=UNKNOWN,
                notes=("no killmails recorded for the corp yet",),
            )

        impact = loss_impact_summary(30)
        loss_patterns = [
            {
                "ship_class": d["name"],
                "losses": d["losses"],
                "doctrine_deviation_pct": (
                    round(100.0 * d["deviated"] / d["losses"]) if d["losses"] else 0
                ),
            }
            for d in impact.get("doctrines", [])[:_TOP_PATTERNS]
        ]
        facts = {
            "efficiency_pct": round(float(head.get("efficiency") or 0.0)),
            "danger_pct": round(100.0 * float((head.get("danger") or {}).get("ratio") or 0.0)),
            "last_30d": last_30d,
            "loss_patterns": loss_patterns,
            "repeat_loss_pilots": len(impact.get("repeat_offenders", [])),
        }
        return SourceSlice(
            key=self.key, version=1, facts=facts, as_of=now_iso(),
            coverage_pct=100.0, status=OK,
        )

    def _window_30d(self) -> dict:
        """The corp's pre-computed 30-day combat rollup (``CombatMetric`` window='30d')."""
        from django.conf import settings

        from apps.killboard.models import CombatMetric

        row = (
            CombatMetric.objects.filter(
                entity_type=CombatMetric.EntityType.CORPORATION,
                entity_id=getattr(settings, "FORCA_HOME_CORP_ID", 0),
                window="30d",
            )
            .values("kills", "losses", "isk_destroyed", "isk_lost")
            .first()
        )
        if not row:
            return {"kills": 0, "losses": 0, "isk_destroyed": 0, "isk_lost": 0}
        return {
            "kills": row["kills"] or 0,
            "losses": row["losses"] or 0,
            "isk_destroyed": isk(row["isk_destroyed"]),
            "isk_lost": isk(row["isk_lost"]),
        }


register_source(CombatSource())
