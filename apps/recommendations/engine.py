"""The recommendation engine: rule-based, explainable evaluators.

Each evaluator reads prepared module data and returns zero or more
Recommendation *drafts* (dicts carrying the common contract). The runner
persists them, superseding prior open recommendations for the same
(type, subject). See handbooks/contributor-handbook/architecture.md §7.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.doctrines.models import Doctrine
from apps.doctrines.services import doctrine_coverage
from apps.industry import bom
from apps.industry.models import IndustryProject, IndustryProjectItem
from apps.killboard.models import Killmail
from apps.market.models import MarketLocation
from apps.market.services import seeding_deficit
from apps.sso.models import EveCharacter
from apps.stockpile.models import HaulingTask
from apps.stockpile.services import shortfalls_against_targets

from . import messages
from .models import Recommendation


def _tname(type_id) -> str:
    """Resolve a type id to its SDE name (for human-readable messages)."""
    from apps.sde.models import SdeType

    return SdeType.objects.filter(type_id=type_id).values_list("name", flat=True).first() or f"#{type_id}"


def _isk(value) -> str:
    """Compact ISK for embedding in messages (1.2B, 340M, 12k)."""
    v = float(value)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}"


def _draft(rec_type, *, subject_type, subject_id, message_key, message_params, logic_key,
           logic_params=None, inputs, confidence, severity,
           permission="officer", action=None, as_of=None, isk_impact=0):
    """Build a draft, persisting the i18n scaffold key + params **and** the English prose.

    The engine runs on the beat, in a Celery worker that has no user and no locale, so the sentence
    cannot be translated here — see ``.messages``. The draft therefore carries both halves:

    * ``message_key``/``message_params`` (and the ``logic_*`` pair): the read-time seam. Params must
      be plain JSON-safe values — ints, strings — because they land in a JSONField, where a
      ``gettext_lazy`` proxy is a hard ``TypeError`` at save time.
    * ``message``/``logic_summary``: the canonical English, derived from the *same* scaffold via
      ``messages.english`` so the prose and the key can never drift apart. This column stays the
      audit record and the fallback, and it is what ``persist_drafts`` compares on — so English
      behaviour, and the idempotency check, are byte-for-byte unchanged.
    """
    message_params = message_params or {}
    logic_params = logic_params or {}
    return {
        "type": rec_type,
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "message": messages.english(message_key, message_params),
        "message_key": message_key,
        "message_params": message_params,
        "logic_summary": messages.english(logic_key, logic_params),
        "logic_summary_key": logic_key,
        "logic_summary_params": logic_params,
        "inputs": inputs,
        "isk_impact": isk_impact,
        "confidence": confidence,
        # Freshness of the data the recommendation rests on. Defaults to now (the
        # evaluator read live), but each evaluator passes the ``as_of`` of its
        # actual source (skills snapshot, market price, killmail…) so a stale feed
        # surfaces as a stale recommendation rather than a falsely-fresh one.
        "data_freshness": as_of or timezone.now(),
        "required_permission": permission,
        "suggested_action": action or {},
        "severity": severity,
    }


def _source_as_of(queryset):
    """The freshest ``as_of`` across a ProvenanceMixin queryset, or now() if empty.

    Cheap (a single MAX aggregate). Used to stamp a recommendation with the
    real freshness of the data feeding it instead of the engine's run time.
    """
    from django.db.models import Max

    return queryset.aggregate(m=Max("as_of"))["m"] or timezone.now()


def _skills_as_of():
    from apps.characters.models import CharacterSkillSnapshot

    return _source_as_of(CharacterSkillSnapshot.objects.filter(is_latest=True))


def _stock_as_of():
    from apps.stockpile.models import StockpileItem

    return _source_as_of(StockpileItem.objects.all())


def _market_as_of():
    from apps.market.models import MarketPrice

    return _source_as_of(MarketPrice.objects.all())


def _killmails_as_of():
    return _source_as_of(Killmail.objects.filter(involves_home_corp=True))


def _corp_member_characters():
    return list(EveCharacter.objects.filter(is_corp_member=True))


def eval_stock_shortage() -> list[dict]:
    from apps.market.pricing import price_for

    out = []
    as_of = _stock_as_of()
    for s in shortfalls_against_targets():
        out.append(
            _draft(
                Recommendation.Type.STOCK_SHORTAGE,
                subject_type="type",
                subject_id=s["type_id"],
                message_key="stock_shortage.message",
                message_params={
                    "type_name": _tname(s["type_id"]),
                    "current": s["current"],
                    "target": s["target"],
                    "deficit": s["deficit"],
                },
                logic_key="stock_shortage.logic",
                inputs=s,
                confidence=Recommendation.Confidence.HIGH,
                severity=min(100, s["deficit"]),
                action={"verb": "acquire", "type_id": s["type_id"], "quantity": s["deficit"]},
                as_of=as_of,
                isk_impact=price_for(s["type_id"]) * s["deficit"],
            )
        )
    return out


def eval_doctrine_readiness() -> list[dict]:
    out = []
    members = _corp_member_characters()
    as_of = _skills_as_of()
    for doctrine in Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).prefetch_related("fits"):
        counts = doctrine_coverage(doctrine, members)
        ready = counts["optimal"] + counts["viable"]
        total = sum(counts.values())
        if total == 0:
            # No members yet — emitting "0/0" with high severity would be noise.
            continue
        confidence = (
            Recommendation.Confidence.HIGH
            if counts["unknown"] == 0
            else Recommendation.Confidence.MEDIUM
        )
        out.append(
            _draft(
                Recommendation.Type.DOCTRINE_READINESS,
                subject_type="doctrine",
                subject_id=doctrine.id,
                message_key="doctrine_readiness.message",
                message_params={"ready": ready, "total": total, "doctrine": doctrine.name},
                logic_key="doctrine_readiness.logic",
                inputs=counts,
                confidence=confidence,
                severity=max(0, 50 - ready * 5),
                action={"verb": "train_or_recruit", "doctrine_id": doctrine.id},
                as_of=as_of,
            )
        )
    return out


def eval_build_vs_buy() -> list[dict]:
    out = []
    as_of = _market_as_of()
    for item in IndustryProjectItem.objects.select_related("project").filter(
        project__status=IndustryProject.Status.ACTIVE
    ):
        d = bom.decide_build_or_buy(item.type_id, item.quantity, item.me)
        if not d["buildable"]:
            continue
        out.append(
            _draft(
                Recommendation.Type.BUILD_VS_BUY,
                subject_type="project_item",
                subject_id=item.id,
                message_key="build_vs_buy.message",
                message_params={
                    "type_name": _tname(item.type_id),
                    "quantity": item.quantity,
                    # The raw decision verb ("build"/"buy") — also ``suggested_action["verb"]``,
                    # which the UI dispatches on, so it stays canonical English.
                    "decision": d["decision"],
                    "build": _isk(d["build_cost"]),
                    "buy": _isk(d["buy_cost"]),
                },
                logic_key="build_vs_buy.logic",
                inputs={"decision": d["decision"], "build": str(d["build_cost"]), "buy": str(d["buy_cost"])},
                confidence=Recommendation.Confidence.MEDIUM,
                severity=20,
                action={"verb": d["decision"], "type_id": item.type_id, "quantity": item.quantity},
                as_of=as_of,
                isk_impact=abs(d["buy_cost"] - d["build_cost"]),
            )
        )
    return out


def eval_market_seeding() -> list[dict]:
    out = []
    # Aggregate by type (max target) so multiple stockpiles don't overwrite.
    targets: dict[int, int] = {}
    for s in shortfalls_against_targets():
        targets[s["type_id"]] = max(targets.get(s["type_id"], 0), s["target"])
    as_of = _market_as_of()
    for loc in MarketLocation.objects.filter(is_staging=True, active=True):
        for type_id, target in targets.items():
            deficit = seeding_deficit(type_id, loc, target)
            if deficit > 0:
                out.append(
                    _draft(
                        Recommendation.Type.MARKET_SEEDING,
                        subject_type="type",
                        subject_id=f"{type_id}@{loc.id}",
                        message_key="market_seeding.message",
                        message_params={
                            "deficit": deficit,
                            "type_name": _tname(type_id),
                            "location": loc.name,
                        },
                        logic_key="market_seeding.logic",
                        inputs={"type_id": type_id, "target": target, "deficit": deficit},
                        confidence=Recommendation.Confidence.MEDIUM,
                        severity=15,
                        action={"verb": "seed", "type_id": type_id, "quantity": deficit},
                        as_of=as_of,
                    )
                )
    return out


def eval_hauling() -> list[dict]:
    open_tasks = HaulingTask.objects.exclude(status=HaulingTask.Status.DONE)
    total_volume = sum(t.volume_m3 for t in open_tasks)
    count = open_tasks.count()
    if count == 0:
        return []
    return [
        _draft(
            Recommendation.Type.HAULING,
            subject_type="logistics",
            subject_id="open",
            message_key="hauling.message",
            # ``volume`` is pre-formatted to a string here (it was an f-string ``:.0f`` before):
            # the rounding is the engine's, not a translator's, and a float in the JSONField would
            # leave the formatting to whatever locale the reader happens to have.
            message_params={"count": count, "volume": f"{total_volume:.0f}"},
            logic_key="hauling.logic",
            inputs={"count": count, "volume_m3": total_volume},
            confidence=Recommendation.Confidence.HIGH,
            severity=min(100, count * 5),
            action={"verb": "haul"},
        )
    ]


def eval_combat_loss_pattern(window_days: int = 7, threshold: int = 3) -> list[dict]:
    out = []
    since = timezone.now() - timedelta(days=window_days)
    losses = Killmail.objects.filter(
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM, killmail_time__gte=since
    )
    by_ship: dict[int, int] = {}
    for km in losses:
        by_ship[km.victim_ship_type_id] = by_ship.get(km.victim_ship_type_id, 0) + 1
    as_of = _killmails_as_of()
    for ship_type_id, n in by_ship.items():
        if n >= threshold:
            out.append(
                _draft(
                    Recommendation.Type.COMBAT_LOSS_PATTERN,
                    subject_type="ship",
                    subject_id=ship_type_id,
                    message_key="combat_loss.message",
                    message_params={
                        "count": n,
                        "ship_name": _tname(ship_type_id),
                        "window_days": window_days,
                    },
                    logic_key="combat_loss.logic",
                    logic_params={"threshold": threshold},
                    inputs={"ship_type_id": ship_type_id, "count": n, "window_days": window_days},
                    confidence=Recommendation.Confidence.MEDIUM if n < 5 else Recommendation.Confidence.HIGH,
                    severity=min(100, n * 10),
                    action={"verb": "review_doctrine", "ship_type_id": ship_type_id},
                    as_of=as_of,
                )
            )
    return out


# The two member-permission evaluators (newbro_next_step, skill_training) were
# retired with the Daily Briefing merge: both duplicated advice the pilot already
# gets from the command_intel quest log and the onboarding Getting-started
# section, via different math that could disagree with them on any given day.
# Migration 0005 expires the rows they left open.
# REC-2 (2.13): the registry keys each evaluator so leadership can enable/disable it and
# tune its thresholds from the console (RecommendationConfig), no deploy required.
EVALUATOR_REGISTRY = [
    ("stock_shortage", "Stock shortage", eval_stock_shortage),
    ("doctrine_readiness", "Doctrine readiness", eval_doctrine_readiness),
    ("build_vs_buy", "Build vs buy", eval_build_vs_buy),
    ("market_seeding", "Market seeding", eval_market_seeding),
    ("hauling", "Hauling backlog", eval_hauling),
    ("combat_loss", "Combat-loss pattern", eval_combat_loss_pattern),
]

ALL_EVALUATORS = [func for _key, _label, func in EVALUATOR_REGISTRY]


@transaction.atomic
def persist_drafts(drafts: list[dict]) -> int:
    """Persist drafts, superseding prior open recs for the same (type, subject).

    **Idempotent on content.** If an open rec for the same ``(type, subject)`` already
    carries the identical ``message``, the draft is a no-op — the existing row (and the
    alert already sent for it) is left untouched. Without this, a rolling-window
    evaluator whose finding hasn't changed (e.g. combat-loss "Lost N × ship in the last
    7d") would supersede-and-recreate an identical NEW rec on every run, and
    ``dispatch_alerts`` — which only skips a rec that already has an alert — would create
    a fresh alert and re-broadcast it every cycle (the 30-minute notification loop).
    A genuine change (e.g. the count moving 5 → 6) alters the message, so it still
    supersedes the prior rec and re-alerts, as intended.
    """
    created = 0
    for d in drafts:
        # Capture prior open recs BEFORE creating a new one, otherwise the lazy
        # queryset would re-evaluate and supersede the row we just made.
        prior = list(
            Recommendation.objects.filter(
                type=d["type"],
                subject_type=d["subject_type"],
                subject_id=d["subject_id"],
                state__in=[Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED],
            )
        )
        # Same finding already open → leave it (and its dispatched alert) alone.
        if any(p.message == d["message"] for p in prior):
            continue
        new_rec = Recommendation.objects.create(state=Recommendation.State.NEW, **d)
        if prior:
            Recommendation.objects.filter(id__in=[p.id for p in prior]).update(
                state=Recommendation.State.SUPERSEDED, superseded_by=new_rec
            )
        created += 1
    return created


def run_all() -> int:
    from .models import RecommendationConfig

    cfg = RecommendationConfig.active()
    disabled = set(cfg.disabled_evaluators or [])
    drafts: list[dict] = []
    for key, _label, evaluator in EVALUATOR_REGISTRY:
        if key in disabled:
            continue  # leadership muted this evaluator
        if key == "combat_loss":
            drafts.extend(evaluator(
                window_days=cfg.combat_loss_window_days, threshold=cfg.combat_loss_threshold
            ))
        else:
            drafts.extend(evaluator())
    if cfg.min_severity:
        drafts = [d for d in drafts if d.get("severity", 0) >= cfg.min_severity]
    return persist_drafts(drafts)
