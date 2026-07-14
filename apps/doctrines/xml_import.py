"""Service layer for the EVE-client XML doctrine importer.

Ties the pure parser (:mod:`apps.doctrines.xml_parser`) to the doctrine domain:

1. ``read_upload`` — validate the uploaded file (extension, size) and return its
   bytes, without our code ever persisting the upload to disk.
2. ``classify_fittings`` — resolve EVE type names against the SDE, validate the
   ship hull, and classify each fitting against the existing library.
3. ``create_batch`` — stage the parsed + classified result in a
   :class:`~apps.doctrines.models.DoctrineImportBatch` (nothing touches the
   Doctrine tables yet).
4. ``commit_batch`` — apply only the director's confirmed per-fitting decisions,
   in a single transaction.

Kept separate from the console views so the whole pipeline is unit-testable
without HTTP, and reuses the shared fit primitives (``fit_signature``,
``create_fit``, ``update_fit``) so this importer and the ESI importer agree on
what "the same fit" means.
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.sde.models import SdeType

from . import xml_parser
from .models import Doctrine, DoctrineFit, DoctrineImportBatch
from .services import create_fit, fit_signature, imported_category, name_conflict, update_fit

# EVE SDE category id for the "Ship" category — a shipType must resolve into it.
SHIP_CATEGORY_ID = 6

_ALLOWED_EXT = ".xml"

# --- Classification statuses -------------------------------------------------
STATUS_NEW = "new"
STATUS_IDENTICAL = "identical"
STATUS_CONFLICT = "conflict"
STATUS_DUPLICATE_FIT = "duplicate_fit"
STATUS_HULL_CONFLICT = "hull_conflict"
STATUS_INVALID = "invalid"

# Display-only labels for the preview table. The KEYS are the persisted status
# codes (stored in DoctrineImportBatch.payload and compared in the template) —
# translate the values only.
STATUS_LABELS = {
    STATUS_NEW: _("New"),
    STATUS_IDENTICAL: _("Identical — skip"),
    STATUS_CONFLICT: _("Conflict"),
    STATUS_DUPLICATE_FIT: _("Possible duplicate fit"),
    STATUS_HULL_CONFLICT: _("Name clash (different hull)"),
    STATUS_INVALID: _("Invalid"),
}
# The statuses the reviewer can act on (the others are automatic).
_ACTIONABLE = {STATUS_NEW, STATUS_CONFLICT, STATUS_DUPLICATE_FIT, STATUS_HULL_CONFLICT}


# --- Upload handling ---------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """A boring, display-only version of the client-supplied filename.

    Strips any path component and anything outside ``[A-Za-z0-9._ -]`` so the
    name can be echoed in the UI or a log line without XSS/log-injection risk. It
    is *never* used to open, store or serve a file.
    """
    base = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    base = base.strip() or "upload.xml"
    return base[:120]


def _scan_for_malware(data: bytes, filename: str) -> None:
    """Extension point for antivirus/malware scanning of the upload bytes.

    No scanner is wired in yet (the hardened parser is the current line of
    defence), so this is intentionally a no-op. To enable, call out to e.g.
    ClamAV (``clamd``) here and raise ``xml_parser.XmlImportError`` on a hit —
    this runs on the in-memory bytes before parsing, so nothing infected is ever
    persisted or acted upon.
    """
    return None


def read_upload(uploaded) -> tuple[bytes, str]:
    """Validate a Django ``UploadedFile`` and return ``(bytes, sanitized_name)``.

    Extension and declared size are checked before any content is read; the read
    itself is bounded to ``MAX_FILE_BYTES + 1`` so a size-spoofed stream cannot
    exhaust memory. We never write the upload to our own disk — Django's upload
    handler owns any transient spool file (random-named, outside the web root,
    auto-deleted at request end) and we never serve or reference it.
    """
    if uploaded is None:
        raise xml_parser.NotXmlError(gettext("No file was uploaded."))
    filename = sanitize_filename(getattr(uploaded, "name", ""))
    if not filename.lower().endswith(_ALLOWED_EXT):
        raise xml_parser.NotXmlError(
            gettext("Only .xml files exported from the EVE client are accepted.")
        )
    declared = getattr(uploaded, "size", None)
    limit_mb = xml_parser.MAX_FILE_BYTES // (1024 * 1024)
    too_large = gettext("The file is larger than the %(limit)s MB limit.") % {"limit": limit_mb}
    if declared is not None and declared > xml_parser.MAX_FILE_BYTES:
        raise xml_parser.FileTooLargeError(too_large)
    data = uploaded.read(xml_parser.MAX_FILE_BYTES + 1)
    if len(data) > xml_parser.MAX_FILE_BYTES:
        raise xml_parser.FileTooLargeError(too_large)
    _scan_for_malware(data, filename)
    return data, filename


# --- Name resolution ---------------------------------------------------------
def _resolve_types(names: set[str]) -> dict[str, dict]:
    """Case-insensitively resolve EVE type names to ``{type_id, category_id,
    name}`` in a single query. Keyed by the lower-cased name."""
    lowered = {n.lower() for n in names if n}
    if not lowered:
        return {}
    rows = (
        SdeType.objects.annotate(lname=Lower("name"))
        .filter(lname__in=lowered)
        .values_list("lname", "type_id", "group__category_id", "name")
    )
    out: dict[str, dict] = {}
    for lname, type_id, category_id, real_name in rows:
        out.setdefault(lname, {"type_id": type_id, "category_id": category_id, "name": real_name})
    return out


def _slot_display(hardware) -> str:
    label = xml_parser.SLOT_LABELS.get(hardware.slot_category, hardware.slot_category or "—")
    if hardware.slot_index is not None:
        return f"{label} {hardware.slot_index}"
    return label


def _agg(modules) -> dict[int, int]:
    out: dict[int, int] = defaultdict(int)
    for m in modules:
        out[int(m["type_id"])] += int(m.get("quantity", 1) or 1)
    return dict(out)


def _diff_lines(incoming_modules, existing_modules) -> list[str]:
    """A short human diff of two module lists (by resolved type), newest first."""
    inc, exi = _agg(incoming_modules), _agg(existing_modules)
    ids = set(inc) | set(exi)
    names = dict(SdeType.objects.filter(type_id__in=ids).values_list("type_id", "name"))
    lines: list[str] = []
    for tid in ids:
        a, b = inc.get(tid, 0), exi.get(tid, 0)
        if a == b:
            continue
        nm = names.get(tid, f"Type {tid}")
        if b == 0:
            lines.append(f"+ {nm} ×{a}")
        elif a == 0:
            lines.append(f"− {nm} ×{b}")
        else:
            lines.append(f"~ {nm} {b} → {a}")
    lines.sort()
    return lines[:12]


def _default_rename(name: str) -> str:
    today = timezone.now().date().isoformat()
    return f"{name} (Imported {today})"[:200]


def _existing_ref(doctrine, fit) -> dict:
    return {
        "doctrine_id": doctrine.id,
        "doctrine_name": doctrine.name,
        "fit_id": fit.id if fit is not None else None,
        "hull_name": (
            SdeType.objects.filter(type_id=fit.ship_type_id).values_list("name", flat=True).first()
            if fit is not None
            else ""
        )
        or "",
    }


# --- Classification ----------------------------------------------------------
def classify_fittings(raw_fittings) -> tuple[list[dict], dict]:
    """Resolve and classify parsed fittings against the existing library.

    Returns ``(entries, counts)`` where each entry is a JSON-serialisable dict
    ready to store on a batch (and to render). No database writes happen here —
    this powers the preview, which must never modify doctrines.
    """
    # One SDE lookup for every ship + item name across the whole file.
    names: set[str] = set()
    for raw in raw_fittings:
        if raw.ship_type_name:
            names.add(raw.ship_type_name)
        for hw in raw.hardware:
            names.add(hw.type_name)
    resolved = _resolve_types(names)

    # Index the existing library once: by name (for name clashes) and by fit
    # signature (for cross-name duplicate detection).
    by_name: dict[str, list] = defaultdict(list)
    sig_to_name: dict[tuple, str] = {}
    for doctrine in Doctrine.objects.prefetch_related("fits"):
        by_name[doctrine.name.lower()].append(doctrine)
        for fit in doctrine.fits.all():
            sig_to_name.setdefault(fit_signature(fit.ship_type_id, fit.modules), doctrine.name)

    entries: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    for index, raw in enumerate(raw_fittings):
        entry = _classify_one(index, raw, resolved, by_name, sig_to_name)
        counts[entry["status"]] += 1
        entries.append(entry)
    counts["total"] = len(entries)
    return entries, dict(counts)


def _classify_one(index, raw, resolved, by_name, sig_to_name) -> dict:
    reasons: list[str] = list(raw.errors)  # parser-level per-fitting problems
    warnings: list[str] = []

    # Resolve the hull (only if the parser found exactly one shipType).
    ship_type_id = None
    ship_name = raw.ship_type_name
    if raw.ship_type_name:
        ship = resolved.get(raw.ship_type_name.lower())
        if ship is None:
            reasons.append(f"Unknown ship type “{raw.ship_type_name}”.")
        elif ship["category_id"] != SHIP_CATEGORY_ID:
            reasons.append(f"“{raw.ship_type_name}” is not a ship hull.")
        else:
            ship_type_id = ship["type_id"]
            ship_name = ship["name"]

    # Resolve hardware into the doctrine module format.
    modules: list[dict] = []
    unresolved: list[str] = []
    for hw in raw.hardware:
        info = resolved.get(hw.type_name.lower())
        if info is None:
            unresolved.append(hw.type_name)
            continue
        modules.append({
            "type_id": info["type_id"],
            "quantity": hw.quantity,
            "slot": _slot_display(hw),
            "name": info["name"],
        })
    if unresolved:
        shown = ", ".join(sorted(set(unresolved))[:6])
        reasons.append(f"Unknown item(s): {shown}.")

    base = {
        "index": index,
        "name": raw.name,
        "ship_name": ship_name,
        "ship_type_id": ship_type_id,
        "modules": modules,
        "item_count": sum(m["quantity"] for m in modules),
        "reasons": reasons,
        "warnings": warnings,
        "diff": [],
        "existing": None,
        "default_rename": _default_rename(raw.name) if raw.name else "",
    }

    if reasons or ship_type_id is None:
        base["status"] = STATUS_INVALID
        return base

    sig = fit_signature(ship_type_id, modules)
    same_name = by_name.get(raw.name.lower(), [])
    if same_name:
        identical = _first_fit(same_name, lambda f: fit_signature(f.ship_type_id, f.modules) == sig)
        if identical:
            base["status"] = STATUS_IDENTICAL
            base["existing"] = _existing_ref(*identical)
            return base
        same_hull = _first_fit(same_name, lambda f: f.ship_type_id == ship_type_id)
        if same_hull:
            base["status"] = STATUS_CONFLICT
            base["existing"] = _existing_ref(*same_hull)
            base["diff"] = _diff_lines(modules, same_hull[1].modules)
            return base
        base["status"] = STATUS_HULL_CONFLICT
        base["existing"] = _existing_ref(same_name[0], None)
        return base

    dup_name = sig_to_name.get(sig)
    if dup_name:
        base["status"] = STATUS_DUPLICATE_FIT
        warnings.append(f"An identical fit already exists as “{dup_name}”.")
    else:
        base["status"] = STATUS_NEW
    return base


def _first_fit(doctrines, predicate):
    """Return ``(doctrine, fit)`` for the first fit matching ``predicate``, else None."""
    for doctrine in doctrines:
        for fit in doctrine.fits.all():
            if predicate(fit):
                return doctrine, fit
    return None


# --- Staging + commit --------------------------------------------------------
def create_batch(user, filename: str, file_size: int, entries: list[dict], counts: dict) -> DoctrineImportBatch:
    return DoctrineImportBatch.objects.create(
        owner=user,
        source_filename=filename,
        file_size=file_size,
        status=DoctrineImportBatch.Status.PREVIEW,
        payload=entries,
        counts=counts,
    )


def _validate_new_name(name: str) -> str:
    name = (name or "").strip()
    return name[:200]


@transaction.atomic
def commit_batch(batch: DoctrineImportBatch, decisions: dict, user) -> dict:
    """Apply the reviewer's confirmed decisions from ``batch.payload``.

    ``decisions`` maps ``str(index) -> {"action": ..., "new_name": ...}``. The
    *authoritative* fit data is read from the batch payload, never from the
    request, so a tampered form can only change the chosen action — not the fit
    that gets written. Runs inside a transaction: any unexpected error rolls the
    whole import back rather than leaving a half-applied library.

    Actions by status:
      * ``new`` / ``duplicate_fit`` → default **import** (may be skipped).
      * ``identical`` → always skipped (no change needed).
      * ``conflict`` → default **skip**; ``rename`` or ``replace`` on request.
      * ``hull_conflict`` → default **skip**; ``rename`` or ``import`` on request.
      * ``invalid`` → always rejected.
    """
    if batch.status != DoctrineImportBatch.Status.PREVIEW:
        return batch.result or {}

    category = imported_category()
    created: list[str] = []
    renamed: list[str] = []
    replaced: list[str] = []
    skipped = 0
    identical = 0
    rejected = 0
    notes: list[str] = []

    for entry in batch.payload:
        status = entry.get("status")
        decision = decisions.get(str(entry.get("index")), {}) or {}
        action = (decision.get("action") or "").strip().lower()

        if status == STATUS_INVALID:
            rejected += 1
            continue
        if status == STATUS_IDENTICAL:
            identical += 1
            continue

        # Fill in the safe default action for this status.
        if status in (STATUS_NEW, STATUS_DUPLICATE_FIT):
            action = action or "import"
        else:  # conflict / hull_conflict
            action = action or "skip"

        if action == "skip":
            skipped += 1
            continue

        ship_type_id = entry.get("ship_type_id")
        modules = entry.get("modules") or []
        source_name = (entry.get("name") or "")[:200]
        if not ship_type_id:  # never import something that didn't fully resolve
            rejected += 1
            continue

        # Replace an existing conflicting doctrine's fit in place (preserves ids).
        if action == "replace" and status == STATUS_CONFLICT and entry.get("existing"):
            doctrine = Doctrine.objects.filter(pk=entry["existing"].get("doctrine_id")).first()
            fit = (
                DoctrineFit.objects.filter(pk=entry["existing"].get("fit_id")).first()
                if doctrine else None
            )
            if doctrine and fit:
                update_fit(fit, name=source_name or fit.name, modules=modules)
                replaced.append(doctrine.name)
                continue
            notes.append(f"“{source_name}”: the doctrine to replace no longer exists — imported as new.")
            action = "import"  # fall through to create

        # Determine the doctrine name for a create/rename.
        if action == "rename":
            target_name = _validate_new_name(decision.get("new_name")) or entry.get("default_rename", "")[:200]
        else:
            target_name = source_name
        if not target_name:
            rejected += 1
            continue

        # Final idempotency guard: never mint an exact duplicate, even under a
        # racing import or a rename that collides. A plain name clash with a
        # *different* fit only blocks the unintended paths — a name-clash import
        # the reviewer explicitly chose ("import anyway") is allowed through, since
        # the doctrine model permits same-named doctrines on different hulls.
        kind, _existing = name_conflict(target_name, ship_type_id, modules)
        if kind == "duplicate":
            identical += 1
            continue
        if kind == "conflict" and status != STATUS_HULL_CONFLICT:
            notes.append(f"“{target_name}” is already used by a different fit — skipped; rename and retry.")
            skipped += 1
            continue

        doctrine = Doctrine.objects.create(
            name=target_name,
            category=category,
            status=Doctrine.Status.ACTIVE,
            description=f"Imported from EVE XML ({batch.source_filename}).",
            created_by=user,
        )
        create_fit(doctrine, name=source_name or target_name, ship_type_id=ship_type_id, modules=modules)
        (renamed if action == "rename" else created).append(doctrine.name)

    result = {
        "created": len(created),
        "renamed": len(renamed),
        "replaced": len(replaced),
        "skipped": skipped,
        "identical": identical,
        "rejected": rejected,
        "created_names": created,
        "renamed_names": renamed,
        "replaced_names": replaced,
        "notes": notes,
    }
    batch.status = DoctrineImportBatch.Status.COMMITTED
    batch.result = result
    batch.committed_at = timezone.now()
    batch.save(update_fields=["status", "result", "committed_at", "updated_at"])
    return result


def is_actionable(status: str) -> bool:
    return status in _ACTIONABLE
