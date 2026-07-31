"""Celery tasks: data-retention enforcement and the CVE response loop.

Detection was never the weak link — the scan found Pillow's 25 advisories and raised a
severity-80 director finding the same day. The finding then sat unread, and stayed
"open" for days after the fix shipped, because nothing pushed it at a human and nothing
re-scanned until the next scheduled run. A control that reliably produces ignored alerts
is worse than none: it manufactures the feeling of coverage. So the loop here is
deliberately closed at both ends — a change reaches a person promptly, and a fix retires
its own finding promptly.

**Two scanners, one response authority.** There are two vulnerability layers with the
same shape: the Python dependencies inside the image (:mod:`apps.admin_audit.dependency_audit`,
scanned here by pip-audit) and the OS packages of the images production is actually
running (:mod:`apps.admin_audit.image_scan`, scanned on the host and piped in). Both need
identical answers to "does this open or close a finding" and "is this worth waking a
human for", so the open/close/transition machinery (:func:`_sync_finding`) and the relay
(:func:`_relay`) are written once and parameterised by subject. A second near-identical
copy would drift, and the copy that drifts is the one that silently stops firing —
nobody notices an alert that does not arrive.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from .services import enforce_member_leave, enforce_retention

log = logging.getLogger("forca.security")

# Stable identity for the single open finding per security control, so a repeat scan
# updates one Recommendation rather than piling up duplicates. ``_REC_SUBJECT_ID`` names
# the dependency audit and is imported by ``health_alert`` — keep the name.
_REC_SUBJECT_TYPE = "security"
_REC_SUBJECT_ID = "dependency_audit"
_IMAGE_REC_SUBJECT_ID = "image_scan"

# What the scan did to the standing finding. Only a *change* is worth a human's
# attention; "unchanged" is the steady state and must stay silent, because a daily
# "still 3 CVEs" ping is exactly how a channel gets muted before the next real one.
TRANSITION_OPENED = "opened"        # clean → vulnerable
TRANSITION_WORSENED = "worsened"    # strictly more CVEs than last time
TRANSITION_CHANGED = "changed"      # the CVE set moved (partial fix, swapped advisory)
TRANSITION_UNCHANGED = "unchanged"  # same finding, still open
TRANSITION_CLEARED = "cleared"      # vulnerable → confirmed clean (a fix landed)
TRANSITION_CLEAN = "clean"          # was clean, still clean
TRANSITION_ERROR = "error"          # the scan could not run — we learned nothing
_RELAY_TRANSITIONS = frozenset({
    TRANSITION_OPENED, TRANSITION_WORSENED, TRANSITION_CHANGED, TRANSITION_CLEARED,
})


@shared_task(name="admin_audit.enforce_retention")
def run_retention() -> dict:
    return enforce_retention()


@shared_task(name="admin_audit.enforce_member_leave")
def run_member_leave() -> dict:
    """Apply the on-member-leave retention policy to departed members' data.

    Ships DISARMED: report-only (counts what it would delete, deletes nothing) until a
    Director arms it on the retention page, so leadership can review the impact first.
    """
    return enforce_member_leave()


@shared_task(name="admin_audit.audit_dependencies")
def audit_dependencies(*, trigger: str = "scheduled", relay: bool = True) -> dict:
    """The whole dependency-CVE response loop: scan → finding → human.

    This is the ONE entry point for the loop. The beat calls it daily; the
    ``audit_dependencies`` management command calls it for CI, manual runs and — the
    reason it must be callable from outside Celery — at the end of a deploy, so a
    release that fixes a CVE clears its own finding within minutes instead of leaving a
    stale "25 known vulnerabilities" alarm standing until the next scheduled run. A
    control that keeps crying wolf after the wolf is dead is how people are trained to
    ignore the next real one.

    ``trigger`` is recorded on the stored result (see ``dependency_audit``);
    ``relay=False`` suppresses the human relay for ephemeral databases (CI), where every
    run looks like a brand-new finding and the transition signal is meaningless.

    Returns the scan summary plus ``transition`` (what changed) and ``relayed``.
    """
    from .dependency_audit import run_dependency_audit

    summary = run_dependency_audit(trigger=trigger)
    transition = _sync_recommendation(summary)
    relayed = _relay(transition) if relay else False
    log.info("dependency audit loop: trigger=%s status=%s transition=%s relayed=%s",
             trigger, summary.get("status"), transition, relayed)
    return {**summary, "transition": transition, "relayed": relayed}


def ingest_image_scan(document: str | bytes, *, trigger: str = "manual",
                      relay: bool = True, error: str = "") -> dict:
    """The image-scan response loop: validate → store → finding → human.

    The mirror image of :func:`audit_dependencies`, and deliberately the same shape, but
    it is a plain function rather than a Celery task because the *scan* half cannot run
    here at all: it needs the Docker daemon, and giving the web container a socket into
    the daemon would be a worse vulnerability than any it could find. So there is no beat
    entry to schedule — the host's systemd timer is the scheduler, and the document
    arrives already-scanned through ``manage.py ingest_image_scan``. A ``@shared_task``
    nothing enqueues would be a scheduling hook that does not schedule anything.

    ``trigger`` records why the host ran the scan; ``relay=False`` suppresses the human
    relay for ephemeral databases (CI), where every run looks like a brand-new finding and
    the transition signal is meaningless; ``error`` records a delivery that never produced
    a document at all (see :func:`~apps.admin_audit.image_scan.ingest_document`).

    Returns the stored summary plus ``transition`` (what changed) and ``relayed``.
    """
    from .image_scan import ingest_document

    summary = ingest_document(document, trigger=trigger, error=error)
    transition = _sync_image_scan_recommendation(summary)
    relayed = _relay(transition, label="image scan") if relay else False
    log.info(
        "image scan ingest: trigger=%s status=%s complete=%s scanned=%s/%s "
        "vulns=%s fixable=%s transition=%s relayed=%s",
        trigger, summary.get("status"), summary.get("complete"),
        summary.get("scanned_count"), summary.get("image_count"),
        summary.get("vuln_count"), summary.get("fixable_count"), transition, relayed,
    )
    return {**summary, "transition": transition, "relayed": relayed}


@shared_task(name="admin_audit.scan_integration_health")
def scan_integration_health() -> dict:
    """ADM-3 (2.2): fire one deduped director alert when a background sync stops, the
    SDE goes stale, or a dependency CVE appears. Deduped + no-op when disabled."""
    from .health_alert import scan_integration_health as _scan

    return _scan()


def _vuln_ids(vulns) -> set[str]:
    """The identity of a vulnerability set: its advisory ids, order-independent.

    Versions and fix lists churn (a rebuild bumps a package, an advisory gains a fix
    version) without the finding actually changing, so identity is the id set alone.

    Scoped by image when the record names one. The image scan reports the same advisory
    once per affected image, and a bare id set would collapse those: "patched in nginx,
    still in web" would read as *unchanged* and never reach anyone. Dependency records
    carry no ``image``, so their identity is unaffected.
    """
    ids: set[str] = set()
    for v in vulns or []:
        vid = str(v.get("id", ""))
        if not vid:
            continue
        scope = str(v.get("image", ""))
        ids.add(f"{scope}:{vid}" if scope else vid)
    return ids


def _relay(transition: str, *, label: str = "dependency audit") -> bool:
    """Push a *changed* finding at a human, within seconds of the scan.

    An on-site Recommendation nobody logs in to read is not a notification — that is the
    whole defect this loop exists to fix. The delivery fabric for that is ADM-3
    (:mod:`apps.admin_audit.health_alert`), which already renders open CVE findings into
    a director alert and fans it out over the Pingboard channels (in-app, EVE-mail,
    verified Telegram/Discord DMs, and any designated leadership chat channel).

    So this deliberately does **not** add a second emitter. ADM-3 already owns the
    dedup-on-problem-set-change logic and the leadership on/off switch; a parallel
    CVE-only broadcaster would double-notify on every finding, which is precisely the
    spam that trains people to mute the channel. What was missing was not an emitter but
    *latency*: ADM-3 only sweeps every 30 minutes and only re-reads a Recommendation the
    weekly scan refreshed. Kicking its (idempotent, signature-deduped) sweep the moment
    the finding set actually moves closes that gap without changing its semantics — an
    unchanged set stays a no-op, and a run where nothing moved never calls it at all.

    Leadership can silence the relay without touching the scan by disabling the
    ``admin_audit.integration_health`` event on the notifications console: the scan still
    runs, the Recommendation and the stored result are still maintained, only the push
    stops. Best-effort by construction — a pingboard fault (unconfigured, provider down,
    tables missing) is logged and swallowed so it can never lose a scan result.

    Shared by both scanners; ``label`` only names the caller in the log, because the whole
    point is that there is one emitter and one dedup authority no matter which control
    moved. NOTE: ADM-3's ``collect_problems`` currently renders only the *dependency*
    finding into its alert body, so an image-scan-only change kicks the sweep and finds
    nothing to say. The kick is still correct and forward-compatible — the moment
    ``collect_problems`` learns about the image finding, the latency fix applies to it
    with no change here — but until then an image finding reaches directors on-site (the
    Recommendation) rather than by push. See the note in ``image_scan``'s ingest command.
    """
    if transition not in _RELAY_TRANSITIONS:
        return False
    try:
        from .health_alert import scan_integration_health

        result = scan_integration_health()
    except Exception:  # noqa: BLE001 — a notification fault must never lose the scan result
        log.exception("%s relay failed (transition=%s)", label, transition)
        return False
    return result.get("status") == "alerted"


def _sync_finding(
    summary: dict,
    *,
    subject_id: str,
    records: list[dict],
    message: str,
    logic_summary: str,
    severity: int = 80,
) -> str:
    """Mirror a scan result into an idempotent, director-visible Recommendation.

    **The single open/close authority for every vulnerability scanner in the app.** On
    findings: create/refresh one open finding. On a confirmed-clean scan: close it. On an
    *error* scan: leave any open finding exactly as it was — a scan that failed, or that
    could not read every target, must never be mistaken for "all clear".

    That last rule is the one worth centralising. It is a single line of code and the only
    thing standing between "the scanner broke" and a green dashboard, and it is precisely
    the kind of line that gets subtly re-implemented, or quietly dropped, in a second copy
    written months later for a second scanner. So the scanners differ only in what they
    *say* (``message``, ``logic_summary``, ``records``) and share every decision about what
    it *means*.

    ``summary["status"]`` is the contract each scanner normalises to: ``vulnerable`` /
    ``ok`` / anything else (treated as an error). The image scan collapses "incomplete"
    into that third bucket at its own boundary for exactly this reason.

    Returns the transition (``opened`` / ``worsened`` / ``changed`` / ``unchanged`` /
    ``cleared`` / ``clean`` / ``error``) so the caller can decide whether a human needs
    to hear about it. The comparison is over advisory ids, not the rendered message.
    """
    from apps.recommendations.models import Recommendation

    open_states = [Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED]
    existing = Recommendation.objects.filter(
        type=Recommendation.Type.OFFICER_ACTION,
        subject_type=_REC_SUBJECT_TYPE,
        subject_id=subject_id,
        state__in=open_states,
    ).first()

    status = summary.get("status")
    if status not in {"ok", "vulnerable"}:
        # An errored (or unrecognised) scan taught us nothing about the deployed image.
        # Leave the surface exactly as it was: never close an open finding, never open
        # one, and never let the caller relay it as news.
        return TRANSITION_ERROR

    if status == "vulnerable":
        fields = {
            "message": message,
            "severity": severity,
            "confidence": Recommendation.Confidence.HIGH,
            "required_permission": "director",
            "logic_summary": logic_summary,
            "inputs": {
                "vulns": records,
                "as_of": summary.get("as_of"),
                "trigger": summary.get("trigger", ""),
            },
            "data_freshness": timezone.now(),
        }
        if not existing:
            Recommendation.objects.create(
                type=Recommendation.Type.OFFICER_ACTION,
                subject_type=_REC_SUBJECT_TYPE,
                subject_id=subject_id,
                **fields,
            )
            return TRANSITION_OPENED

        before = _vuln_ids((existing.inputs or {}).get("vulns"))
        after = _vuln_ids(records)
        if after == before:
            transition = TRANSITION_UNCHANGED
        elif after > before:  # strict superset — new advisories on top of the known set
            transition = TRANSITION_WORSENED
        else:
            transition = TRANSITION_CHANGED

        for key, value in fields.items():
            setattr(existing, key, value)
        if transition != TRANSITION_UNCHANGED:
            # Re-surface only on a genuine change. A director who acknowledged a finding
            # they are actively fixing must not have it flipped back to NEW by every
            # nightly re-scan — daily nagging on an unchanged finding is the same
            # cry-wolf failure as a daily notification, just on-site.
            existing.state = Recommendation.State.NEW
        existing.save()
        return transition

    if existing:
        # Confirmed clean — retire the finding. This is the branch a deploy that ships
        # the fix depends on: it is why the loop is re-run at the end of a release.
        existing.state = Recommendation.State.SUPERSEDED
        existing.closed_at = timezone.now()
        existing.save(update_fields=["state", "closed_at", "updated_at"])
        return TRANSITION_CLEARED
    return TRANSITION_CLEAN


def _sync_recommendation(summary: dict) -> str:
    """The dependency audit's finding: what to tell a director, then :func:`_sync_finding`."""
    vulns = summary.get("vulns", [])
    detail = "; ".join(
        f"{v['name']} {v['version']} → {v['id']}"
        + (f" (fix: {', '.join(v['fix_versions'])})" if v["fix_versions"] else " (no fix yet)")
        for v in vulns[:20]
    )
    n = summary.get("vuln_count", len(vulns))
    return _sync_finding(
        summary,
        subject_id=_REC_SUBJECT_ID,
        # Clipped at 50: the finding is a call to action, not an archive, and the stored
        # row already holds the full (capped) list.
        records=vulns[:50],
        message=(
            f"{n} known vulnerabilit{'y' if n == 1 else 'ies'} in installed "
            f"dependencies. Bump the affected package(s) and redeploy. {detail}"
        ),
        logic_summary=(
            "Daily pip-audit scan of the running container's installed dependencies "
            "(also re-run at the end of every deploy)."
        ),
    )


def _image_scan_message(summary: dict) -> str:
    """What a director needs in order to act on an image finding.

    Deliberately parallel to the dependency message — same shape, same register — because
    the two land side by side in the same list and a reader should not have to learn two
    formats. The differences are the ones an operator actually acts on: *which* images
    (the fix is a base-image bump per image, not a package bump), how many findings are
    fixable **today** (an advisory with no upstream fix cannot be actioned by bumping
    anything, and pretending otherwise sends someone on a fruitless hunt), and which
    images could not be read at all, since findings from a partial scan describe part of
    the deployment and saying so is the difference between a report and a claim.
    """
    fixable = summary.get("fixable_count", 0)
    total = summary.get("vuln_count", 0)
    per_image = "; ".join(
        f"{', '.join(img['services']) or img['image']} ({img['os'] or 'unknown base'}): "
        f"{img['vuln_count']} finding(s), {img['fixable_count']} fixable"
        for img in summary.get("images", [])[:10] if img.get("vuln_count")
    )
    detail = "; ".join(
        f"{v['severity']} {v['id']} in {v['package']} {v['version']} [{v['image']}]"
        + (f" (fix: {', '.join(v['fix_versions'])})" if v["fix_versions"] else " (no fix yet)")
        for v in summary.get("vulns", [])[:20]
    )
    if fixable:
        head = (
            f"{fixable} fixable OS-package vulnerabilit{'y' if fixable == 1 else 'ies'} "
            f"(of {total}) in the container image(s) production is running. Rebuild on a "
            "current base image — or bump the pinned tag in docker-compose.prod.yml for a "
            "third-party image such as nginx or postgres — and redeploy."
        )
    else:
        head = (
            f"{total} OS-package vulnerabilit{'y' if total == 1 else 'ies'} in the container "
            "image(s) production is running, none with an upstream fix yet. Nothing to bump "
            "today; recorded so the next base-image rebuild is an informed decision."
        )
    unscanned = "; ".join(
        f"{', '.join(u['services']) or u['image']} — {u['error']}"
        for u in summary.get("unscannable", [])[:10]
    )
    parts = [head]
    if per_image:
        parts.append(f"Affected: {per_image}.")
    if unscanned:
        # Named loudly: these findings describe only what could be read.
        parts.append(f"NOT SCANNED (so not covered by the counts above): {unscanned}.")
    if detail:
        parts.append(detail)
    return " ".join(parts)


def _sync_image_scan_recommendation(summary: dict) -> str:
    """The image scan's finding: what to tell a director, then :func:`_sync_finding`."""
    return _sync_finding(
        summary,
        subject_id=_IMAGE_REC_SUBJECT_ID,
        records=summary.get("vulns", [])[:50],
        message=_image_scan_message(summary),
        # Severity tracks actionability: fixable findings are work someone can start now;
        # a set with no released fix anywhere is a watch item, and ranking it as urgently
        # as an actionable one is how a severity scale stops meaning anything.
        severity=80 if summary.get("fixable_count", 0) else 60,
        logic_summary=(
            "Daily trivy scan (on the host) of the OS packages in the container images "
            "the running containers were actually started from."
        ),
    )
