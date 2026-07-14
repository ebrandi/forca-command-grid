"""ADM-3 (roadmap 2.2) — proactive integration-health & CVE alerting.

The integration-health page (:mod:`apps.admin_audit.health`) and the weekly
dependency audit are both *pull-only*: a stopped background sync, a stale SDE
build, or a fresh dependency vulnerability only surfaces if a director happens to
open the page. This fires **one deduped director alert** into the Pingboard fabric
when the set of problems changes, and resets when the corp returns to healthy (so a
later recurrence alerts again).

Scoped deliberately to *infrastructure* health — stopped beats, stale SDE, and
dependency CVEs. The complementary per-token "your ingestion token just died"
nudge is SSO-2 / roadmap 2.1's job (it names the exact character + scope to
re-auth); keeping token specifics out of here avoids double-alerting on the same
root cause when a dead token later shows up as a stale feed.
"""
from __future__ import annotations

import hashlib
import logging

from django.utils import timezone

from .models import AppSetting

log = logging.getLogger("forca.security")

_EVENT_KEY = "admin_audit.integration_health"
# Where the last-alerted problem-set signature lives (dedup state; no migration).
_SIG_SETTING_KEY = "health_alert:sig"


def _ago(dt) -> str:
    if not dt:
        return "never"
    from django.utils.timesince import timesince

    return f"{timesince(dt)} ago"


def _open_cve() -> dict | None:
    """The current open dependency-vulnerability finding (maintained weekly by
    ``audit_dependencies``), or ``None`` when there is none / it was resolved."""
    from apps.recommendations.models import Recommendation

    from .tasks import _REC_SUBJECT_ID, _REC_SUBJECT_TYPE

    rec = Recommendation.objects.filter(
        subject_type=_REC_SUBJECT_TYPE,
        subject_id=_REC_SUBJECT_ID,
        state__in=[Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED],
    ).first()
    if rec is None:
        return None
    vulns = (rec.inputs or {}).get("vulns", []) or []
    ids = sorted({str(v.get("id", "")) for v in vulns if v.get("id")})
    return {"count": len(vulns), "ids": ids}


def collect_problems() -> list[dict]:
    """Current integration-health problems: stopped beats, stale SDE, open CVEs.

    Only *stale* beats (recorded a success before, now past their staleness
    threshold) count — a never-recorded ("missing") beat is a brand-new/never-run
    sync, not a breakage, so it never alerts. This also keeps a fresh deploy from
    false-firing, since the generous per-beat thresholds (12h+) far exceed any
    restart blip.
    """
    from .health import beat_health, sde_health

    problems: list[dict] = []
    for b in beat_health():
        if b["status"] == "stale":
            problems.append({
                "kind": "beat", "key": b["key"], "label": b["label"],
                "detail": f"{b['label']} last succeeded {_ago(b['last'])} — the sync has stopped.",
            })
    sde = sde_health()
    if sde["status"] == "stale":
        problems.append({
            "kind": "sde", "key": "sde", "label": "SDE static data",
            "detail": f"SDE build {sde.get('version') or '?'} loaded {_ago(sde['loaded_at'])} "
                      "— run import_sde_fuzzwork to refresh.",
        })
    cve = _open_cve()
    if cve and cve["count"]:
        # Prefer the distinct-CVE-id count for the wording (matches the ids shown);
        # fall back to the raw entry count if pip-audit gave entries without ids.
        n = len(cve["ids"]) or cve["count"]
        shown = ", ".join(cve["ids"][:8]) or "see the dependency finding"
        problems.append({
            "kind": "cve", "key": "cve:" + ",".join(cve["ids"]),
            "label": "Dependency vulnerabilities",
            "detail": f"{n} dependency vulnerabilit{'y' if n == 1 else 'ies'} found "
                      f"({shown}{'…' if n > 8 else ''}) — bump the affected package(s) and redeploy.",
        })
    return problems


def _signature(problems: list[dict]) -> str:
    """A stable hash of the *identity* of the current problem set (order-independent),
    so an unchanged set is a no-op and any new/removed problem re-alerts once."""
    canonical = "|".join(sorted(f"{p['kind']}:{p['key']}" for p in problems))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _emit(problems: list[dict], sig: str) -> bool:
    lines = "\n".join(f"• {p['detail']}" for p in problems)
    body = (
        "FORCA integration health has degraded:\n\n" + lines +
        "\n\nOpen the Admin Console → Health page for the full picture. This alert "
        "fires once per distinct problem set and resets automatically once healthy."
    )
    # This function is only reached when our AppSetting-signature dedup already
    # decided this is a genuinely new/changed/recurring problem set — so make the
    # pingboard-level keys unique per firing (stamp them). A purely sig-based key
    # would let pingboard's own idempotency/duplicate guards silently swallow a
    # legitimate recurrence after a recovery (same sig → same key → old row reused).
    # Microsecond precision: two sequential firings are always distinct.
    stamp = int(timezone.now().timestamp() * 1_000_000)
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.notifications import resolve

        # Honour the console audience override (default director) rather than hardcoding
        # it, so leadership widening the audience on the notifications console takes
        # effect. The dispatch-layer classification guard still enforces the ceiling.
        audience_kind = resolve(_EVENT_KEY).get("audience") or "director"
        alert = pingboard.emit_broadcast(
            category="custom",
            title="Integration health degraded",
            body=body,
            # Scaffold + raw context: the digest chrome localises per recipient; the problem
            # lines are diagnostic data and stay raw. ``body`` is the frozen English audit column.
            template="admin_audit.integration_health",
            context={"details": lines},
            audience={"kind": audience_kind},
            source_service="admin_audit",
            source_object_id=f"integration_health:{sig[:16]}:{stamp}",
            idempotency_key=f"admin_audit:integration_health:{sig[:16]}:{stamp}",
        )
        # None == pingboard suppressed it (globally disabled / held for approval): report
        # not-sent so the caller does NOT burn the dedup slot and retries next run.
        return alert is not None
    except Exception:  # noqa: BLE001 — a notification fault must never break the scan
        log.exception("integration-health alert failed")
        return False


def scan_integration_health() -> dict:
    """Fire one deduped director alert when the integration-health problem set changes.

    Deduped on a signature of the current problem set stored in ``AppSetting``: an
    unchanged set is a no-op, and a return to healthy clears the stored signature so
    a later recurrence alerts again. No-op when leadership disables the event on the
    notifications console.
    """
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_EVENT_KEY):
        return {"status": "disabled"}

    problems = collect_problems()
    stored = AppSetting.objects.filter(key=_SIG_SETTING_KEY).first()

    if not problems:
        if stored is not None:
            stored.delete()  # recovered → reset dedup so a recurrence re-alerts
        return {"status": "ok"}

    sig = _signature(problems)
    if stored is not None and (stored.value or {}).get("sig") == sig:
        return {"status": "unchanged", "problems": len(problems)}

    # Only burn the dedup slot once the alert is genuinely accepted by pingboard. If
    # the emit is suppressed (pingboard globally disabled / held for approval) or
    # errors, leave the signature unset so the next 30-min run retries — a persistent
    # failure can't spam (nothing was delivered), and a stuck sync is never silently
    # forgotten.
    if not _emit(problems, sig):
        return {"status": "alert_failed", "problems": len(problems)}
    AppSetting.objects.update_or_create(
        key=_SIG_SETTING_KEY,
        defaults={"value": {
            "sig": sig, "at": timezone.now().isoformat(),
            "problems": [p["key"] for p in problems],
        }},
    )
    return {"status": "alerted", "problems": len(problems)}
