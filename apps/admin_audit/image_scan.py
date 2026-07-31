"""Container-image vulnerability scan — the *storage* half of a host-side control.

The scan itself cannot run here. Scanning the images that production is actually
running means talking to the Docker daemon, and mounting ``/var/run/docker.sock`` into
the web container would hand every code-execution foothold in the application root on
the host — strictly worse than any advisory the scan could ever find. So the flow is
one-directional: :file:`scripts/scan-running-images.sh` runs trivy on the host, builds a
scan document (:file:`scripts/image_scan_report.py` owns that schema), and pipes it into
``manage.py ingest_image_scan``. The app never reaches up.

That inversion is what makes this module's job *validation*. Unlike the dependency audit
— which shells out to pip-audit itself and therefore knows what produced its input —
everything here arrives on **stdin from another process**. The document is untrusted:
possibly truncated mid-write, possibly from a newer script than this code, possibly
self-contradictory because trivy died halfway. The one outcome that must be impossible is
a bad document reading as a clean bill of health and closing an open director finding.
Every rule below exists to make "we do not know" the failure mode instead of "all clear".

WHAT COUNTS AS CLEAN
--------------------
Three separate things must all hold before a run is stored as ``ok``:

* the document parses, matches the schema we understand, and is internally consistent;
* it found nothing (``vuln_count == 0``);
* it is **complete** — every running image was actually scanned.

The third is the one that is easy to get wrong and the reason ``complete`` exists in the
schema at all. A running container can outlive its own image (a rebuild moves the tag,
``docker image prune`` reaps the untagged original) and go on serving happily from an
already-unpacked rootfs that nothing can scan any more. A scan that skipped it learned
nothing about it, and reporting "0 findings" then shrinks the denominator and calls the
remainder the whole truth. So an incomplete run is stored as ``error`` with a reason: not
clean, not a finding, simply unknown — and, critically, unable to close anything.

Pure-ish, like :mod:`apps.admin_audit.dependency_audit`: this validates, derives and
writes one ``AppSetting`` row. Everything that *responds* to the result — the director
Recommendation and the human relay — lives one layer up in :mod:`apps.admin_audit.tasks`
and is shared with the dependency audit, so there is one response authority rather than
two copies of "should this alert fire" drifting apart.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .dependency_audit import (
    TRIGGER_DEPLOY,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
    result_freshness,
)
from .models import AppSetting

log = logging.getLogger("forca.security")

__all__ = [
    "FRESHNESS_MAX_AGE_HOURS",
    "IMAGE_SCAN_SETTING_KEY",
    "MAX_DOCUMENT_CHARS",
    "SCHEMA_VERSION",
    "TRIGGERS",
    "ingest_document",
    "read_document",
    "scan_freshness",
]

# Where the latest image-scan result is stored (read by the ops/health surface).
IMAGE_SCAN_SETTING_KEY = "security:image_scan"

# The only scan-document schema this code knows how to read. A *newer* document is
# rejected rather than best-effort parsed: the whole value of this row is that its
# meaning is unambiguous, and guessing at fields whose semantics may have changed is how
# a control starts quietly reporting the wrong thing. An operator sees the error, on the
# scan console and in the deploy log, and updates the app.
SCHEMA_VERSION = 1

TRIGGERS = (TRIGGER_SCHEDULED, TRIGGER_DEPLOY, TRIGGER_MANUAL)

STATUS_OK = "ok"
STATUS_VULNERABLE = "vulnerable"
STATUS_ERROR = "error"
_DOCUMENT_STATUSES = frozenset({STATUS_OK, STATUS_VULNERABLE, STATUS_ERROR})

# The host scanner runs from a daily systemd timer (04:20, see scripts/systemd/), so this
# matches the dependency audit's 36h for the same reason: one missed run (host reboot,
# trivy DB fetch failure) must NOT flip the surface to "unknown" — otherwise the staleness
# signal is noise and gets ignored — but two consecutive misses must. Stamped into every
# row so a consumer reading an old row judges it against the cadence that produced it.
FRESHNESS_MAX_AGE_HOURS = 36.0

# Storage caps. The scan document is already clipped at 200 findings by the host reporter,
# but that is the *producer* being polite and this is untrusted input, so the limits are
# re-applied here rather than assumed. Matching the dependency audit's thinking: exact
# counts, clipped lists, and a flag that says the list was clipped.
_MAX_REPORTED = 100      # findings kept in the stored row
_MAX_IMAGES = 40         # image / unscannable records kept
_MAX_FIELD = 200         # characters per scalar field
_MAX_LIST = 12           # entries per nested list (services, containers, fix_versions)
# A hard ceiling on what we will even read from stdin. Well above any honest document
# (four running images at 200 findings is tens of kilobytes) and low enough that a runaway
# or hostile producer cannot make the ingest process itself the outage.
MAX_DOCUMENT_CHARS = 4 * 1024 * 1024


def _text(value, limit: int = _MAX_FIELD) -> str:
    """Any JSON scalar as a bounded string. Coerces rather than rejects: a wrong *type*
    in one cosmetic field is not worth discarding a real finding over, and the length cap
    means even a pathological value cannot bloat the row."""
    if value is None:
        return ""
    return str(value)[:limit]


def _texts(value, limit: int = _MAX_LIST, item_limit: int = _MAX_FIELD) -> list[str]:
    """A bounded list of bounded strings, for services / containers / fix_versions.

    Both bounds matter, and the list bound matters most: these lists are rendered *inside*
    a per-finding line, so an uncapped one multiplies against the finding cap and turns a
    100-finding row into a megabyte of text.
    """
    if not isinstance(value, list):
        return []
    return [_text(v, item_limit) for v in value[:limit]]


def _validate_shape(doc) -> str:
    """Reject anything we cannot read as a schema-1 scan document. Returns a reason.

    Every field the later logic *branches on* is type-checked here, so no downstream rule
    can be skipped by a field arriving as the wrong type. In particular ``complete`` must
    be a real boolean: a missing or non-boolean value would otherwise be falsy and could
    be argued either way, and "the producer forgot to say" must never resolve to "yes,
    everything was scanned".
    """
    if not isinstance(doc, dict):
        return "scan document is not a JSON object"

    schema = doc.get("schema")
    if schema != SCHEMA_VERSION:
        return f"unsupported scan document schema {schema!r} (this app reads {SCHEMA_VERSION})"

    if doc.get("status") not in _DOCUMENT_STATUSES:
        return f"unrecognised scan status {doc.get('status')!r}"

    if not isinstance(doc.get("complete"), bool):
        return "scan document does not state whether it scanned everything"

    for key in ("image_count", "scanned_count", "vuln_count", "fixable_count"):
        count = doc.get(key)
        # bool is an int subclass in Python; a True here means a broken producer.
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return f"scan document field {key!r} is not a count"

    for key in ("images", "unscannable", "vulns"):
        rows = doc.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return f"scan document field {key!r} is not a list of records"

    return ""


def _validate_consistency(doc: dict) -> str:
    """Reject a document that contradicts itself. Returns a reason.

    A truncated or half-written document usually fails to parse, which is easy. The
    dangerous case is one that parses *and* lies — trivy killed after writing the counts,
    a producer bug, a hand-edited file — because a document claiming ``status: "ok"`` with
    findings still in the list would otherwise close a live director finding.

    So rather than trusting any single field, the counts, the lists, the completeness flag
    and the headline status are cross-checked against each other. They are all derived
    from the same scan by :file:`scripts/image_scan_report.py`, so in an honest document
    they cannot disagree; if they do, we know only that we cannot trust the producer, and
    the safe reading of that is "unknown".
    """
    status = doc["status"]
    complete = doc["complete"]
    images, unscannable, vulns = doc["images"], doc["unscannable"], doc["vulns"]
    n_vulns, n_fixable = doc["vuln_count"], doc["fixable_count"]
    n_images, n_scanned = doc["image_count"], doc["scanned_count"]
    truncated = bool(doc.get("truncated"))

    if n_scanned > n_images:
        return "scanned more images than it set out to scan"
    if len(images) != n_scanned:
        return "the scanned-image list does not match scanned_count"
    if n_fixable > n_vulns:
        return "more fixable findings than findings"
    # The producer clips the list at its own cap and sets ``truncated``; so an untruncated
    # document lists exactly what it counted, and a truncated one lists strictly fewer.
    if truncated and len(vulns) >= n_vulns:
        return "marked truncated but nothing was clipped"
    if not truncated and len(vulns) != n_vulns:
        return "the finding list does not match vuln_count"

    if status == STATUS_OK and n_vulns:
        return "reports a clean scan alongside findings"
    if status == STATUS_VULNERABLE and not n_vulns:
        return "reports findings but lists none"
    if status == STATUS_ERROR and images:
        return "reports that nothing could be scanned but also lists scanned images"
    if complete and unscannable:
        return "claims a complete scan but names targets it could not scan"
    if complete and (not n_scanned or n_scanned != n_images):
        return "claims a complete scan but did not scan every image"

    return ""


def _derive_status(doc: dict) -> tuple[str, str]:
    """What this run actually established. Returns ``(status, reason)``.

    The document's own ``status`` answers "what did we find in the images we read", which
    is not the same question as "what does this run let us say about production". The two
    diverge in exactly one place and it is the important one: a run that found nothing but
    could not scan every image is ``ok`` by the first question and *unknown* by the
    second. Collapsing that into ``error`` here — one derivation, at the boundary — is
    what lets every downstream consumer (the stored row, the freshness read model, the
    Recommendation sync) keep the dependency audit's simple ok/vulnerable/error contract
    without each having to remember the completeness rule separately.

    Note the ordering: findings outrank incompleteness. Vulnerabilities found in the images
    we *did* read are real and actionable regardless of what we missed, so they are still
    raised — the message names the unscanned images alongside them.
    """
    if doc["status"] == STATUS_ERROR:
        return STATUS_ERROR, "the scanner could not examine a single image"
    if doc["vuln_count"] > 0:
        return STATUS_VULNERABLE, ""
    if not doc["complete"]:
        missing = [_who(row) for row in doc["unscannable"][:_MAX_IMAGES]]
        return STATUS_ERROR, (
            "no findings, but "
            + (", ".join(missing) or f"{doc['image_count'] - doc['scanned_count']} image(s)")
            + " could not be scanned — this run says nothing about them"
        )
    return STATUS_OK, ""


def _who(row: dict) -> str:
    """"web, worker: forca:prod" — how an operator refers to an image, by what runs it."""
    services = ", ".join(_texts(row.get("services")))
    image = _text(row.get("image")) or "?"
    return f"{services}: {image}" if services else image


def _image_records(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows[:_MAX_IMAGES]:
        out.append({
            "image": _text(row.get("image")),
            "image_id": _text(row.get("image_id")),
            "services": _texts(row.get("services")),
            "containers": _texts(row.get("containers")),
            "os": _text(row.get("os")),
            "vuln_count": _count(row.get("vuln_count")),
            "fixable_count": _count(row.get("fixable_count")),
        })
    return out


def _unscannable_records(rows: list[dict]) -> list[dict]:
    return [{
        "image": _text(row.get("image")),
        "image_id": _text(row.get("image_id")),
        "services": _texts(row.get("services")),
        "containers": _texts(row.get("containers")),
        "error": _text(row.get("error"), 400),
    } for row in rows[:_MAX_IMAGES]]


def _vuln_records(rows: list[dict]) -> list[dict]:
    """Findings, normalised and clipped.

    The list arrives sorted worst-first and fixable-first (the producer's ordering, chosen
    so that clipping drops the things nobody can action today rather than the ones someone
    could fix this afternoon), so a plain head-slice keeps the right ones.
    """
    return [{
        "id": _text(row.get("id"), 64),
        "severity": _text(row.get("severity"), 16).upper(),
        "package": _text(row.get("package"), 128),
        "version": _text(row.get("version"), 64),
        "fix_versions": _texts(row.get("fix_versions"), 6, 64),
        "image": _text(row.get("image")),
        "services": _texts(row.get("services")),
        "url": _text(row.get("url"), 300),
    } for row in rows[:_MAX_REPORTED] if row.get("id")]


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _effective_as_of(doc_as_of: str, now):
    """The timestamp the freshness clock runs from — never later than ingest.

    The document carries the host's own ``as_of``, which is the honest scan time and is
    what an operator wants to see. But it is also attacker-and-accident controlled input,
    and a stamp in the future would make a stale result read as permanently fresh — the
    precise failure this row exists to prevent. Clamping to ingest time keeps the useful
    case (a genuine scan a few seconds ago) exact, keeps a *replayed old document* honestly
    old, and makes a bogus future stamp harmless.
    """
    parsed = parse_datetime(doc_as_of) if doc_as_of else None
    if parsed is None:
        return now
    if timezone.is_naive(parsed):
        # The host writes UTC; a stamp without an offset is read as UTC rather than as
        # local time, so a server in a non-UTC zone cannot shift the age by hours.
        parsed = timezone.make_aware(parsed, UTC)
    return min(parsed, now)


def _persist(summary: dict) -> None:
    AppSetting.objects.update_or_create(key=IMAGE_SCAN_SETTING_KEY, defaults={"value": summary})


def _error_summary(now, trigger: str, reason: str) -> dict:
    """A rejected document still writes a row — silence would be indistinguishable from
    "the scanner never ran", and an operator needs to see that delivery is arriving and
    being refused, with the reason, rather than nothing at all."""
    return {
        "as_of": now.isoformat(),
        "tool": "trivy",
        "trigger": trigger,
        "max_age_hours": FRESHNESS_MAX_AGE_HOURS,
        "stale_after": (now + timedelta(hours=FRESHNESS_MAX_AGE_HOURS)).isoformat(),
        "status": STATUS_ERROR,
        "document_status": "",
        "complete": False,
        "error": reason[:400],
        "host": "", "source": "", "scanner_version": "",
        "severity_floor": "", "pkg_types": "", "scanned_at": "",
        "image_count": 0, "scanned_count": 0, "vuln_count": 0, "fixable_count": 0,
        "truncated": False,
        "images": [], "unscannable": [], "vulns": [],
    }


def read_document(stream, *, limit: int = MAX_DOCUMENT_CHARS) -> tuple[str, str]:
    """Read at most ``limit`` characters of a scan document. Returns ``(text, error)``.

    Capped rather than slurped: this is fed by a pipe from another process, and an
    unbounded read turns a runaway producer into an out-of-memory kill of the web
    container — a worse outage than anything the scan reports. Reading one character past
    the limit is what distinguishes "a big but legal document" from "too big", so the cap
    can be enforced without guessing.
    """
    try:
        text = stream.read(limit + 1)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return "", f"could not read the scan document: {exc}"
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            return "", f"scan document is not valid UTF-8: {exc}"
    if len(text) > limit:
        return "", f"scan document exceeds {limit} characters — refusing to read it"
    return text, ""


def ingest_document(document: str | bytes, *, trigger: str = TRIGGER_MANUAL,
                    error: str = "") -> dict:
    """Validate one scan document, persist a summary to ``AppSetting``, and return it.

    Never raises. Every rejection path — unparseable, wrong schema, self-contradictory —
    produces ``status='error'`` with a human-readable ``error``, because the caller is a
    security control: it must keep working, and an error must stay distinguishable from a
    clean result so it can never retire an open finding.

    ``trigger`` is the *caller's* assertion of why this ran (systemd timer, deploy, an
    operator at a terminal) and is what gets stored. The document carries the host's own
    idea of the trigger too; when they disagree, the caller wins and the difference is
    recorded, since the flag is the one an ops surface can actually trust.

    ``error`` lets a caller that could not even *obtain* a document say so in its own
    words (an unreadable pipe, a missing file) and still have the attempt recorded. A
    delivery that is arriving and failing has to look different from a scanner that never
    ran, or the fix is looked for in the wrong place.
    """
    now = timezone.now()
    if trigger not in TRIGGERS:
        trigger = TRIGGER_MANUAL
    if error:
        return _reject(now, trigger, error)

    if isinstance(document, bytes):
        try:
            document = document.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _reject(now, trigger, f"scan document is not valid UTF-8: {exc}")

    try:
        doc = json.loads(document)
    except (ValueError, TypeError) as exc:
        # The common shape of this is a document truncated mid-write (the scanner was
        # killed, the pipe closed, the disk filled). It must land here, not anywhere near
        # the clean branch.
        return _reject(now, trigger, f"scan document is not valid JSON: {exc}")

    reason = _validate_shape(doc) or _validate_consistency(doc)
    if reason:
        return _reject(now, trigger, reason)

    status, status_reason = _derive_status(doc)
    vulns = _vuln_records(doc["vulns"])
    summary = {
        "as_of": _effective_as_of(_text(doc.get("as_of"), 64), now).isoformat(),
        "ingested_at": now.isoformat(),
        "tool": _text(doc.get("scanner"), 32) or "trivy",
        "scanner_version": _text(doc.get("scanner_version"), 32),
        "trigger": trigger,
        "document_trigger": _text(doc.get("trigger"), 32),
        "max_age_hours": FRESHNESS_MAX_AGE_HOURS,
        "stale_after": (now + timedelta(hours=FRESHNESS_MAX_AGE_HOURS)).isoformat(),
        # ``status`` is what this run *established* (see _derive_status); ``document_status``
        # preserves what the scanner itself said, so the two can be compared when an
        # incomplete-but-otherwise-clean run is reported as unknown.
        "status": status,
        "document_status": doc["status"],
        "complete": doc["complete"],
        "error": status_reason,
        "host": _text(doc.get("host"), 128),
        "source": _text(doc.get("source"), 64),
        "severity_floor": _text(doc.get("severity_floor"), 32),
        "pkg_types": _text(doc.get("pkg_types"), 32),
        "image_count": doc["image_count"],
        "scanned_count": doc["scanned_count"],
        "vuln_count": doc["vuln_count"],
        "fixable_count": doc["fixable_count"],
        # The producer's clip flag OR ours — a consumer only needs to know that the list
        # below is shorter than the counts above, not which layer did the clipping.
        "truncated": bool(doc.get("truncated")) or len(vulns) < doc["vuln_count"],
        "images": _image_records(doc["images"]),
        "unscannable": _unscannable_records(doc["unscannable"]),
        "vulns": vulns,
    }

    if status == STATUS_VULNERABLE:
        log.error(
            "image scan found %d OS-package vulnerabilit%s (%d fixable) across %d/%d image(s): %s",
            summary["vuln_count"], "y" if summary["vuln_count"] == 1 else "ies",
            summary["fixable_count"], summary["scanned_count"], summary["image_count"],
            ", ".join(f"{v['id']} in {v['image']}" for v in vulns[:20]),
        )
    elif status == STATUS_ERROR:
        log.error("image scan incomplete: %s", status_reason)
    else:
        log.info("image scan clean: %d image(s), no fixable HIGH/CRITICAL OS findings",
                 summary["scanned_count"])
    _persist(summary)
    return summary


def _reject(now, trigger: str, reason: str) -> dict:
    log.error("image scan document rejected (trigger=%s): %s", trigger, reason)
    summary = _error_summary(now, trigger, reason)
    _persist(summary)
    return summary


def scan_freshness(value: dict | None = None) -> dict:
    """:func:`~apps.admin_audit.dependency_audit.result_freshness` for the image scan.

    Same read model as the dependency audit — deliberately, so the ops page renders both
    controls with one set of rules — plus the coverage detail that is specific to scanning
    a *set* of images: how many of them were actually read, and how many findings can be
    fixed today by bumping a base image.
    """
    if value is None:
        value = AppSetting.get(IMAGE_SCAN_SETTING_KEY) or {}

    return result_freshness(
        value,
        default_max_age_hours=FRESHNESS_MAX_AGE_HOURS,
        extra={
            "vuln_count": int(value.get("vuln_count") or 0),
            "fixable_count": int(value.get("fixable_count") or 0),
            "image_count": int(value.get("image_count") or 0),
            "scanned_count": int(value.get("scanned_count") or 0),
            "complete": bool(value.get("complete")),
            "unscannable": value.get("unscannable") or [],
            "host": value.get("host") or "",
            "trigger": value.get("trigger") or "",
            "error": value.get("error") or "",
        },
    )
