"""Hardened parser for EVE-client fitting XML exports.

This module is **pure and DB-free** on purpose: it turns fully-untrusted upload
bytes into a list of :class:`RawFitting` structures (names, not resolved type
ids) and does nothing else, so it can be unit-tested and reasoned about in
isolation from the doctrine model. Name→type-id resolution, conflict detection
and persistence all live in :mod:`apps.doctrines.xml_import`.

Security posture (see docs/design/doctrine-xml-import.md):

* Parsing goes through :mod:`defusedxml` with DTD, entity and external-reference
  processing disabled — this defeats XXE, the "billion laughs" entity-expansion
  bomb and SSRF-via-external-entity.
* A raw-byte pre-scan *independently* rejects a DOCTYPE/DTD, entity definitions
  and stray processing instructions before the parser ever runs, so the safety
  does not rest on a single library (defence in depth).
* Every unbounded dimension is capped: file size, number of fittings, hardware
  entries per fitting, nesting depth, text-field lengths and quantity magnitude.
* The element/attribute schema is an allow-list; anything unexpected is rejected
  rather than ignored.

The EVE client export shape this accepts::

    <fittings>
      <fitting name="…">
        <description value="…"/>          (optional)
        <shipType value="…"/>             (exactly one)
        <hardware slot="hi slot 0" type="…" qty="…"/>   (qty optional → 1)
        …
      </fitting>
    </fittings>
"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _defused_fromstring

# --- Hard limits (also surfaced in the UI/docs; see LIMITS) -------------------
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB — a genuine export of thousands of fits is well under this.
# The number of fittings accepted per file is leadership-configurable
# (DoctrineImportConfig); this is the absolute safety ceiling the config can never
# exceed, and the default used when no explicit limit is passed.
MAX_FITTINGS_CEILING = 1000
DEFAULT_MAX_FITTINGS = MAX_FITTINGS_CEILING
MAX_HARDWARE_PER_FIT = 500
MAX_DEPTH = 8
MAX_NAME_LEN = 100  # fitting name (EVE caps fitting names ~50)
MAX_TYPE_LEN = 128  # ship / item type name
MAX_SLOT_LEN = 64
MAX_QTY = 1_000_000

LIMITS = {
    "max_file_bytes": MAX_FILE_BYTES,
    "max_fittings_ceiling": MAX_FITTINGS_CEILING,
    "max_hardware_per_fit": MAX_HARDWARE_PER_FIT,
    "max_depth": MAX_DEPTH,
    "max_name_len": MAX_NAME_LEN,
    "max_type_len": MAX_TYPE_LEN,
    "max_slot_len": MAX_SLOT_LEN,
    "max_qty": MAX_QTY,
}


def clamp_max_fittings(value: int | None) -> int:
    """Clamp a requested per-import fitting limit into ``[1, MAX_FITTINGS_CEILING]``.

    ``None`` (or anything unparseable) falls back to the default. The ceiling is
    a hard safety bound the caller can never exceed, so a mis-set config cannot
    turn the importer into a DoS vector.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FITTINGS
    return max(1, min(value, MAX_FITTINGS_CEILING))


# --- Exceptions --------------------------------------------------------------
class XmlImportError(Exception):
    """Base class. ``str(exc)`` is a safe, user-facing rejection reason (it never
    contains raw uploaded bytes)."""


class FileTooLargeError(XmlImportError):
    pass


class NotXmlError(XmlImportError):
    """Not XML at all (binary, empty, wrong content)."""


class MalformedXmlError(XmlImportError):
    """Well-formedness failure from the parser."""


class ForbiddenConstructError(XmlImportError):
    """A DTD, entity definition, external reference or processing instruction."""


class LimitExceededError(XmlImportError):
    """A count/size limit (fittings, hardware, depth) was exceeded."""


class SchemaError(XmlImportError):
    """Unexpected root, element or attribute — not a genuine EVE export."""


# --- Parsed structures -------------------------------------------------------
@dataclass
class RawHardware:
    slot_raw: str
    slot_category: str  # high|med|low|rig|subsystem|service|drone|fighter|cargo
    slot_index: int | None
    type_name: str
    quantity: int


@dataclass
class RawFitting:
    name: str
    ship_type_name: str
    hardware: list[RawHardware] = field(default_factory=list)
    # Per-fitting problems that make this ONE fitting invalid without sinking the
    # rest of the file (missing name/shipType, bad qty, unknown slot, over-length).
    errors: list[str] = field(default_factory=list)


# --- Slot handling -----------------------------------------------------------
SLOT_LABELS = {
    "high": "High",
    "med": "Mid",
    "low": "Low",
    "rig": "Rig",
    "subsystem": "Subsystem",
    "service": "Service",
    "drone": "Drone Bay",
    "fighter": "Fighter Bay",
    "cargo": "Cargo",
}

_SLOT_INDEXED = [
    (re.compile(r"^hi(?:gh)?\s*slot\s*(\d+)$"), "high"),
    (re.compile(r"^med(?:ium)?\s*slot\s*(\d+)$"), "med"),
    (re.compile(r"^low\s*slot\s*(\d+)$"), "low"),
    (re.compile(r"^rig\s*slot\s*(\d+)$"), "rig"),
    (re.compile(r"^subsystem\s*slot\s*(\d+)$"), "subsystem"),
    (re.compile(r"^service\s*slot\s*(\d+)$"), "service"),
]
_SLOT_EXACT = {
    "drone bay": ("drone", None),
    "cargo": ("cargo", None),
    "fighter bay": ("fighter", None),
    "fighter tube": ("fighter", None),
}


def normalize_slot(raw: str) -> tuple[str | None, int | None]:
    """Map an EVE slot string to ``(category, index)`` or ``(None, None)``.

    ``"hi slot 0"`` → ``("high", 0)``; ``"drone bay"`` → ``("drone", None)``.
    """
    s = raw.strip().lower()
    if s in _SLOT_EXACT:
        return _SLOT_EXACT[s]
    for pattern, category in _SLOT_INDEXED:
        match = pattern.match(s)
        if match:
            return category, int(match.group(1))
    return None, None


# --- Helpers -----------------------------------------------------------------
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value: str) -> str:
    """Strip control characters and surrounding whitespace, preserving the
    visible text otherwise (so a doctrine name reads exactly as authored)."""
    return _CONTROL.sub("", value or "").strip()


def _localname(tag) -> str | None:
    """Local element/attribute name, namespace stripped. ``None`` for the
    function-valued tags ElementTree uses for comments and PIs."""
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1]


def _reject_attrs(el, allowed: tuple[str, ...]) -> None:
    for key in el.attrib:
        if _localname(key) not in allowed:
            raise SchemaError(f"unexpected attribute “{_localname(key)}” on <{_localname(el.tag)}>")


def _reject_children(el) -> None:
    for child in el:
        if _localname(child.tag) is not None:  # ignore comments/PIs
            raise SchemaError(f"<{_localname(el.tag)}> must not contain child elements")


def _check_depth(el, depth: int = 1) -> None:
    if depth > MAX_DEPTH:
        raise LimitExceededError(f"XML nesting is too deep (limit {MAX_DEPTH})")
    for child in el:
        if _localname(child.tag) is not None:
            _check_depth(child, depth + 1)


def _prescan(data: bytes) -> None:
    """Byte-level safety checks that run *before* the XML parser.

    Rejects oversize input, obvious binaries, and — independently of defusedxml —
    any DOCTYPE/DTD, entity definition or stray processing instruction. The only
    ``<?…?>`` tolerated is a leading XML declaration.
    """
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLargeError(
            f"file is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
        )
    if not data.strip():
        raise NotXmlError("the file is empty")
    if b"\x00" in data:
        raise NotXmlError("the file is not text (looks like a binary file)")

    lowered = data.lower()
    if b"<!doctype" in lowered:
        raise ForbiddenConstructError("a DOCTYPE/DTD is not allowed")
    if b"<!entity" in lowered:
        raise ForbiddenConstructError("entity definitions are not allowed")

    stripped = data.lstrip()
    if stripped.startswith(codecs.BOM_UTF8):
        stripped = stripped[len(codecs.BOM_UTF8):].lstrip()
    remainder = stripped
    if stripped[:5].lower() == b"<?xml":
        end = stripped.find(b"?>")
        if end == -1:
            raise MalformedXmlError("the XML declaration is not terminated")
        remainder = stripped[end + 2:]
    if b"<?" in remainder:
        raise ForbiddenConstructError("processing instructions are not allowed")
    if not remainder.lstrip().startswith(b"<"):
        raise NotXmlError("the file does not look like XML")


def _safe_parse(data: bytes):
    try:
        return _defused_fromstring(
            data, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except DefusedXmlException as exc:
        raise ForbiddenConstructError(
            "the file uses an unsafe XML construct (DTD, entity or external reference)"
        ) from exc
    except ParseError as exc:
        raise MalformedXmlError("the file is not well-formed XML") from exc
    except (ValueError, TypeError) as exc:  # defensive: odd encodings etc.
        raise MalformedXmlError("the file could not be parsed as XML") from exc


def _parse_qty(raw: str | None, errors: list[str], type_name: str) -> int | None:
    if raw is None or str(raw).strip() == "":
        return 1  # missing qty means one, per the EVE export convention
    try:
        qty = int(str(raw).strip())
    except (ValueError, TypeError):
        errors.append(f"“{type_name[:40]}” has a non-numeric quantity")
        return None
    if qty < 1:
        errors.append(f"“{type_name[:40]}” has a quantity below 1")
        return None
    if qty > MAX_QTY:
        errors.append(f"“{type_name[:40]}” has an implausibly large quantity")
        return None
    return qty


def _parse_hardware(el, errors: list[str]) -> RawHardware | None:
    slot_raw = _clean_text(el.get("slot") or "")
    type_name = _clean_text(el.get("type") or "")
    if len(slot_raw) > MAX_SLOT_LEN:
        errors.append("a slot name is too long")
        return None
    if not type_name:
        errors.append("a hardware entry is missing its type")
        return None
    if len(type_name) > MAX_TYPE_LEN:
        errors.append("an item name is too long")
        return None
    qty = _parse_qty(el.get("qty"), errors, type_name)
    if qty is None:
        return None
    category, index = normalize_slot(slot_raw)
    if category is None:
        errors.append(f"unknown slot “{slot_raw[:40]}”")
        return None
    return RawHardware(slot_raw, category, index, type_name, qty)


def _parse_fitting(el) -> RawFitting:
    _reject_attrs(el, allowed=("name",))
    errors: list[str] = []

    name = _clean_text(el.get("name") or "")
    if not name:
        errors.append("the fitting has no name")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"the fitting name exceeds {MAX_NAME_LEN} characters")
        name = name[:MAX_NAME_LEN]

    ship_names: list[str] = []
    hardware: list[RawHardware] = []
    for sub in el:
        tag = _localname(sub.tag)
        if tag is None:  # comment / PI object
            continue
        if tag == "description":
            _reject_attrs(sub, allowed=("value",))
            _reject_children(sub)
        elif tag == "shipType":
            _reject_attrs(sub, allowed=("value",))
            _reject_children(sub)
            ship_names.append(_clean_text(sub.get("value") or ""))
        elif tag == "hardware":
            _reject_attrs(sub, allowed=("slot", "type", "qty"))
            _reject_children(sub)
            parsed = _parse_hardware(sub, errors)
            if parsed is not None:
                hardware.append(parsed)
            if len(hardware) > MAX_HARDWARE_PER_FIT:
                raise LimitExceededError(
                    f"a fitting has more than {MAX_HARDWARE_PER_FIT} hardware entries"
                )
        else:
            raise SchemaError(f"unexpected <{tag}> inside a <fitting>")

    if len(ship_names) != 1 or not ship_names[0]:
        errors.append("the fitting must have exactly one ship type")
        ship_name = ship_names[0] if ship_names else ""
    else:
        ship_name = ship_names[0]
    if len(ship_name) > MAX_TYPE_LEN:
        errors.append("the ship type name is too long")
        ship_name = ship_name[:MAX_TYPE_LEN]

    return RawFitting(name=name, ship_type_name=ship_name, hardware=hardware, errors=errors)


def parse_fittings_xml(data: bytes, *, max_fittings: int | None = None) -> list[RawFitting]:
    """Parse and structurally validate EVE fitting XML into ``RawFitting`` rows.

    ``max_fittings`` caps how many fittings the file may contain; it is clamped to
    ``[1, MAX_FITTINGS_CEILING]`` and defaults to the ceiling. Pass the
    leadership-configured value (``DoctrineImportConfig``) here.

    Raises an :class:`XmlImportError` subclass for a file-level rejection (unsafe
    construct, malformed XML, wrong schema, or a breached count/size limit). A
    problem confined to a single fitting (no name, unknown item, bad quantity …)
    is recorded on that fitting's ``errors`` list instead, so the remaining
    fittings can still be reviewed and imported.
    """
    limit = clamp_max_fittings(max_fittings)
    _prescan(data)
    root = _safe_parse(data)
    _check_depth(root)

    if _localname(root.tag) != "fittings":
        raise SchemaError("the root element must be <fittings>")
    _reject_attrs(root, allowed=())

    fittings: list[RawFitting] = []
    for child in root:
        tag = _localname(child.tag)
        if tag is None:  # comment / PI
            continue
        if tag != "fitting":
            raise SchemaError(f"unexpected <{tag}> under <fittings>")
        fittings.append(_parse_fitting(child))
        if len(fittings) > limit:
            raise LimitExceededError(
                f"the file contains more than {limit} fittings"
            )

    if not fittings:
        raise SchemaError("no <fitting> elements were found in the file")
    return fittings
