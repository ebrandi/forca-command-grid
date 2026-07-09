"""Archive retrieval for the conversational interface (P7, doc 10 §7, doc 17 §3).

Lexical, classification-filtered retrieval over CI's OWN structured archive — finished
reports, computed constraints, decided COAs, campaigns and calibration — never raw
operational data (the "consume finished intelligence" principle, doc 10 §7). Every passage
the retriever returns is one the asker is already cleared to see: reports (and the
constraints/COAs derived from them) are filtered through ``access.visible_reports``, so a
grounded answer over these passages inherits the report classification gate. No
embeddings/vector store — the archive is small and already summarised, so a keyword +
recency score over it is enough and fully explainable.
"""
from __future__ import annotations

import logging
import re

from . import access

logger = logging.getLogger("forca.command_intel")

_WORD_RE = re.compile(r"[a-z0-9_]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were",
    "we", "our", "us", "do", "did", "does", "what", "which", "how", "why", "when", "who",
    "can", "could", "should", "would", "will", "have", "has", "had", "with", "at", "it",
    "this", "that", "these", "those", "be", "as", "by", "from", "about", "any",
}
_REPORTS_SCANNED = 15
_COAS_SCANNED = 40
_CAMPAIGNS_SCANNED = 15


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 2 and w not in _STOP}


def _isk(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}"


def _score(passage_text: str, terms: set[str]) -> int:
    """Count of distinct query terms that appear in the passage (lexical overlap)."""
    if not terms:
        return 0
    hay = (passage_text or "").lower()
    return sum(1 for t in terms if t in hay)


def _candidates(user) -> list[dict]:
    """Every archive passage ``user`` is cleared to see (classification-filtered)."""
    from . import outcomes
    from .models import Campaign, CourseOfAction, OperationalConstraint

    # The FULL set of report ids the asker may see (decoupled from the display cap below),
    # so a COA/campaign on an older-but-visible report is still eligible — and, critically,
    # so nothing tied to a report above clearance is ever eligible.
    visible_report_ids = set(access.visible_reports(user).values_list("pk", flat=True))
    reports = list(access.visible_reports(user).order_by("-created_at")[:_REPORTS_SCANNED])
    out: list[dict] = []

    for r in reports:
        body = r.body or {}
        risks = " ".join(
            x.get("risk", "") for x in (body.get("strategic_risks") or []) if isinstance(x, dict)
        )
        text = " ".join(filter(None, [
            r.summary, body.get("executive_summary", ""), body.get("forecast", ""), risks,
        ]))
        out.append({
            "id": f"report:{r.pk}", "kind": "report",
            "title": r.title or f"Report #{r.pk}", "text": text,
            "ref_url": f"/command/reports/{r.pk}/", "recency": 1.0,
        })

    # Constraints from the newest visible report's snapshot (the "now" state).
    newest_sid = next((r.snapshot_id for r in reports if r.snapshot_id), None)
    if newest_sid is not None:
        for c in OperationalConstraint.objects.filter(snapshot_id=newest_sid, status="computed"):
            metric = f"{c.binding_metric} {c.unit}".strip() if c.binding_metric is not None else ""
            out.append({
                "id": f"constraint:{c.key}", "kind": "constraint",
                "title": c.label, "text": f"{c.label}. {metric}. {c.detail}",
                "ref_url": "/command/constraints/", "recency": 0.5,
            })

    # Decided/proposed COAs whose report the asker may see. A report-less COA (e.g. one
    # orphaned when a classified report was deleted — the FK is SET_NULL) has no clearance
    # of its own, so it is EXCLUDED rather than leaked to every officer.
    for coa in CourseOfAction.objects.order_by("-created_at")[:_COAS_SCANNED]:
        if coa.report_id is None or coa.report_id not in visible_report_ids:
            continue
        text = f"{coa.objective}. {coa.reasoning} Decision: {coa.get_state_display()}. {coa.decision_note}"
        ref = f"/command/reports/{coa.report_id}/" if coa.report_id else "/command/"
        out.append({
            "id": f"coa:{coa.pk}", "kind": "coa",
            "title": coa.objective[:80], "text": text, "ref_url": ref, "recency": 0.4,
        })

    for camp in Campaign.objects.order_by("-created_at")[:_CAMPAIGNS_SCANNED]:
        # A campaign derived from a report above the asker's clearance is not surfaced
        # (a campaign can summarise director-tier COAs). Report-less campaigns stay —
        # they carry no classification and match the officer-level campaign board.
        if camp.created_from_report_id is not None and camp.created_from_report_id not in visible_report_ids:
            continue
        out.append({
            "id": f"campaign:{camp.pk}", "kind": "campaign", "title": camp.objective[:80],
            "text": f"{camp.objective}. Status {camp.get_status_display()}, {camp.progress_pct}% complete.",
            "ref_url": f"/command/campaigns/{camp.pk}/", "recency": 0.4,
        })

    for cal in outcomes.calibration_summary():
        out.append({
            "id": f"calibration:{cal['family']}", "kind": "calibration",
            "title": f"Calibration: {cal['family']}",
            "text": (f"Action family {cal['family']}: {cal['n']} measured outcomes, bias "
                     f"{cal['bias']}, error spread {cal['spread']}, confidence factor {cal['factor']}."),
            "ref_url": "/command/", "recency": 0.2,
        })

    out.extend(_combat_candidates(user))
    return out


def _combat_candidates(user) -> list[dict]:
    """Killboard combat passages: recent battles, their AARs (clearance-gated), the rollup."""
    from apps.killboard.models import BattleReport
    from apps.sde.models import SdeSolarSystem

    from . import config
    from .models import BattleAnalysis

    bcfg = config.get("battle")
    out: list[dict] = []
    battles = list(BattleReport.objects.order_by("-start_time")[: bcfg.get("recent_battles_scanned", 20)])
    if battles:
        sys_ids = {s for b in battles for s in (b.system_ids or [])}
        sys_name = dict(
            SdeSolarSystem.objects.filter(system_id__in=sys_ids).values_list("system_id", "name")
        )
        aar: dict[int, BattleAnalysis] = {}
        for a in (
            BattleAnalysis.objects.filter(
                battle_report_id__in=[b.pk for b in battles],
                status__in=[BattleAnalysis.Status.READY, BattleAnalysis.Status.READY_DEGRADED],
            ).order_by("battle_report_id", "-created_at")
        ):
            aar.setdefault(a.battle_report_id, a)
        for b in battles:
            where = ", ".join(sys_name.get(s, f"system {s}") for s in (b.system_ids or []))
            sides = b.sides.get("corporations", []) if isinstance(b.sides, dict) else []
            text = (f"Battle {b.title} in {where}. {len(sides)} side(s); "
                    + "; ".join(f"{s.get('losses', 0)} losses / {s.get('kills', 0)} kills" for s in sides[:4]))
            out.append({
                "id": f"battle:{b.pk}", "kind": "battle", "title": b.title or f"Battle #{b.pk}",
                "text": text, "ref_url": f"/command/battles/{b.pk}/", "recency": 0.5,
            })
            an = aar.get(b.pk)
            if an is not None and access.can_view_report(user, an):
                body = an.body or {}
                atext = " ".join([
                    body.get("summary", ""),
                    *(body.get("what_went_wrong") or []),
                    *(body.get("what_to_improve") or []),
                ])
                out.append({
                    "id": f"battle_analysis:{an.pk}", "kind": "battle AAR",
                    "title": f"AAR: {b.title or b.pk}", "text": atext,
                    "ref_url": f"/command/battles/{b.pk}/", "recency": 0.6,
                })

    # The corp combat-performance rollup: windowed SHIP counts (kills/losses) + ISK + loss
    # patterns, so "how many ships did we lose in the last 30 days" grounds on a real number.
    try:
        from django.conf import settings

        from apps.killboard.analytics import loss_impact_summary, summary
        from apps.killboard.models import CombatMetric

        head = summary()
        window_days = int(bcfg.get("recent_losses_days", 30))
        loss = loss_impact_summary(window_days)
        patterns = ", ".join(
            f"{d.get('name')} x{d.get('losses')}" for d in (loss.get("doctrines") or [])[:5]
        )
        windows = {
            w["window"]: w
            for w in CombatMetric.objects.filter(
                entity_type=CombatMetric.EntityType.CORPORATION,
                entity_id=getattr(settings, "FORCA_HOME_CORP_ID", 0),
                window__in=["7d", "30d"],
            ).values("window", "kills", "losses", "isk_destroyed", "isk_lost")
        }

        def _window_text(w: str) -> str:
            d = windows.get(w)
            if not d:
                return f"Last {w}: no rollup computed yet."
            return (f"Last {w}: {d['losses'] or 0} ships lost, {d['kills'] or 0} ships killed, "
                    f"{_isk(d['isk_lost'])} ISK lost, {_isk(d['isk_destroyed'])} ISK destroyed.")

        out.append({
            "id": "combat:performance", "kind": "combat", "title": "Combat performance",
            "text": (
                f"Corp combat performance. All-time: {head.get('kills', 0)} ships killed / "
                f"{head.get('losses', 0)} ships lost, {round(float(head.get('efficiency') or 0))}% "
                f"ISK efficiency. {_window_text('7d')} {_window_text('30d')} "
                f"In the last {window_days} days, {loss.get('total_deviated', 0)} losses were "
                f"off-doctrine. Loss patterns by ship class: {patterns}."
            ),
            "ref_url": "/killboard/stats/", "recency": 0.3,
        })
    except Exception:  # noqa: BLE001 - combat rollup is best-effort context, never fail retrieval
        logger.exception("command_intel combat rollup passage failed")

    return out


def retrieve(question: str, user, *, k: int = 8) -> list[dict]:
    """Top-``k`` classification-cleared archive passages most relevant to ``question``.

    Deterministic: keyword overlap + a small recency prior. Falls back to the most recent
    passages when nothing lexically matches, so an answer always has grounded context (or
    the caller can see there is none). Each passage carries a stable ``id`` the answer cites.
    """
    terms = _terms(question)
    scored = []
    for p in _candidates(user):
        overlap = _score(f"{p['title']} {p['text']}", terms)
        # Rank by keyword overlap; recency only breaks ties and orders the fallback.
        scored.append((overlap, overlap + p.get("recency", 0.0), p))
    scored.sort(key=lambda t: t[1], reverse=True)
    hits = [p for overlap, _rank, p in scored if overlap >= 1][:k]
    if not hits:  # nothing lexically matched — hand back the freshest context rather than nothing
        hits = [p for _o, _rank, p in scored][:k]
    return hits
