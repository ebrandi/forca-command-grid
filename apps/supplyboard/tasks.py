"""Supply Command board beat: warm the cache + fire the officer problem-set digest."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.supplyboard")


@shared_task(name="supplyboard.sweep")
def sweep() -> dict:
    """Warm the board cache and fire one deduped officer digest on a red-set change.

    INERT behind ``BoardConfig.sweep_enabled`` — the board stays viewable on demand, but
    nothing warms or pings until armed. Stamps ``record_sync`` when armed."""
    from apps.admin_audit.health import record_sync

    from .board import board_data
    from .models import BoardConfig

    if not BoardConfig.active().sweep_enabled:
        return {"status": "disabled"}
    data = board_data(refresh=True)  # the warm
    try:
        result = _fire_digest(data)
    except Exception:  # noqa: BLE001 — a digest fault must not skip the freshness stamp
        log.exception("supply board digest failed")
        result = {"status": "digest_failed", "problems": 0}
    record_sync("supplyboard", problems=result.get("problems", 0))
    return result


def _fire_digest(data: dict) -> dict:
    """Compose the red-row problem set across OFFICER sections and fire the digest.

    Problem keys are the rows' stable pk-based ``key``s, so a red condition that flips once
    (overdue / past-due / below threshold) enters the set once and same-day sweeps with no
    state change produce the identical signature — the 20-minute cadence only re-warms.

    Owner-exclusion is audience-aware, never name-based: the shortages family is the MRP
    beat's own officer ping while ``MrpConfig.auto_run_enabled`` is True, so it becomes a
    count in ``details`` then; while the MRP beat is disarmed, shortages are problem keys.
    Buyer/hauler DMs never suppress a family. Margin/erosion is director-lane only and never
    enters the officer digest.
    """
    from apps.industry.models import MrpConfig
    from apps.pingboard.dedup import fire_on_change

    mrp_owned = MrpConfig.active().auto_run_enabled
    problems: list[str] = []
    owned_counts: dict[str, int] = {}
    for section in data["sections"]:
        if section.role != "officer":
            continue
        red = [r for r in section.rows if r.severity == "red"]
        if not red:
            continue
        if section.key == "shortages" and mrp_owned:
            owned_counts[section.key] = len(red)
            continue
        problems.extend(r.key for r in red)

    # Dedup across sections: one NetRequirement can be BOTH an overdue shortage and a
    # late-feasible bottleneck (same `req:{pk}` key in two sections) — it is one problem,
    # counted once, and the deduped set makes the signature deterministic.
    problems = sorted(set(problems))
    total = len(problems)
    detail_bits = [f"{total} open problem(s)"]
    for key, count in owned_counts.items():
        detail_bits.append(f"{count} {key} (owner-pinged)")
    details = ", ".join(detail_bits)
    result = fire_on_change(
        event_key="supplyboard.digest",
        sig_key="supplyboard:digest:sig",
        problems=problems,
        title="Supply Command digest",
        body=f"Supply Command: {details}. Review the board: /supply-board/",
        source_service="supplyboard",
        source_prefix="digest",
        template="supplyboard.digest",
        context={"count": total, "details": details, "link": "/supply-board/"},
    )
    return {"digest": result, "problems": total}
