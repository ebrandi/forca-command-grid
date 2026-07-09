"""Report output schema + structural validation + entity grounding (design doc 06 §4, ADR-0009).

The LLM returns a single JSON object (the report ``body``). Before any of it is
persisted it passes two gates:

1. **Structural** — required sections present, fields the right type, enums valid.
2. **Entity grounding** — every ``constraint_key`` the model cites must be a real
   computed constraint, and every ``entity_ref`` must exist in an index built from
   the snapshot. Ungrounded items are reported so the engine can repair-retry and,
   failing that, DROP them (nothing ungrounded is ever shown).

Pure Python (no ``jsonschema`` dependency) so the gate is thin and dependency-free.
"""
from __future__ import annotations

EFFORTS = {"low", "medium", "high"}
SEVERITIES = {"info", "watch", "high", "critical"}

# The shape the model is asked to return (documentation; enforced by validate_structure).
REPORT_SCHEMA = {
    "executive_summary": "str",
    "operational_picture": {"posture_statement": "str", "highlights": "list", "not_assessed": "list"},
    "operational_constraints": [{"constraint_key": "str", "interpretation": "str", "priority_rank": "int"}],
    "courses_of_action": [{
        "constraint_key": "str", "objective": "str", "reasoning": "str",
        "risk_if_ignored": "str", "severity_if_ignored": "enum", "effort": "enum",
        "priority": "int", "depends_on": "list", "entity_refs": "list",
    }],
    "strategic_risks": [{"risk": "str", "severity": "enum", "linked_constraint": "str"}],
    "forecast": "str",
    "annexes": "list",
}


def validate_structure(obj) -> list[str]:
    """Return a list of structural violations ("" if valid)."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["report must be a JSON object"]

    if not isinstance(obj.get("executive_summary"), str) or not obj.get("executive_summary", "").strip():
        errs.append("executive_summary must be a non-empty string")

    op = obj.get("operational_picture")
    if not isinstance(op, dict):
        errs.append("operational_picture must be an object")
    else:
        if not isinstance(op.get("posture_statement", ""), str):
            errs.append("operational_picture.posture_statement must be a string")
        for k in ("highlights", "not_assessed"):
            if k in op and not isinstance(op[k], list):
                errs.append(f"operational_picture.{k} must be a list")

    cons = obj.get("operational_constraints", [])
    if not isinstance(cons, list):
        errs.append("operational_constraints must be a list")
    else:
        for i, c in enumerate(cons):
            if not isinstance(c, dict) or not isinstance(c.get("constraint_key"), str):
                errs.append(f"operational_constraints[{i}].constraint_key must be a string")

    coas = obj.get("courses_of_action", [])
    if not isinstance(coas, list):
        errs.append("courses_of_action must be a list")
    else:
        for i, c in enumerate(coas):
            if not isinstance(c, dict):
                errs.append(f"courses_of_action[{i}] must be an object")
                continue
            if not isinstance(c.get("objective"), str) or not c.get("objective", "").strip():
                errs.append(f"courses_of_action[{i}].objective must be a non-empty string")
            if not isinstance(c.get("constraint_key"), str):
                errs.append(f"courses_of_action[{i}].constraint_key must be a string")
            if c.get("effort") not in EFFORTS:
                errs.append(f"courses_of_action[{i}].effort must be one of {sorted(EFFORTS)}")
            sev = c.get("severity_if_ignored")
            if sev is not None and sev not in SEVERITIES:
                errs.append(f"courses_of_action[{i}].severity_if_ignored invalid: {sev!r}")

    if "forecast" in obj and not isinstance(obj["forecast"], str):
        errs.append("forecast must be a string")
    return errs


def build_index(snapshot: dict, constraints: list[dict]) -> dict:
    """Build the grounding index: valid constraint keys + entity names/slugs."""
    constraint_keys = {c.get("key") for c in constraints if c.get("key")}
    entities: set[str] = set()
    sources = snapshot.get("sources") or snapshot.get("slices") or {}
    doctrine = sources.get("doctrine") or {}
    for d in doctrine.get("doctrines", []) or []:
        for field in ("name", "slug"):
            if d.get(field):
                entities.add(str(d[field]))
                entities.add(f"doctrine:{d[field]}")
    for h in doctrine.get("hull_shortfalls_top", []) or []:
        if h.get("type"):
            entities.add(str(h["type"]))
        if h.get("name"):
            entities.add(str(h["name"]))
    infra = sources.get("infrastructure") or {}
    for s in infra.get("low_fuel_structures", []) or []:
        if s.get("name"):
            entities.add(str(s["name"]))
    return {"constraint_keys": constraint_keys, "entities": entities}


def ground(obj: dict, index: dict) -> list[str]:
    """Return grounding violations: cited keys/entities absent from the snapshot."""
    errs: list[str] = []
    keys = index.get("constraint_keys", set())
    entities = index.get("entities", set())
    for i, c in enumerate(obj.get("operational_constraints", []) or []):
        ck = c.get("constraint_key")
        if ck and ck not in keys:
            errs.append(f"operational_constraints[{i}] cites unknown constraint {ck!r}")
    for i, c in enumerate(obj.get("courses_of_action", []) or []):
        ck = c.get("constraint_key")
        if ck and ck not in keys:
            errs.append(f"courses_of_action[{i}] cites unknown constraint {ck!r}")
        for ref in c.get("entity_refs", []) or []:
            if ref not in entities:
                errs.append(f"courses_of_action[{i}] cites unknown entity {ref!r}")
    for i, r in enumerate(obj.get("strategic_risks", []) or []):
        lc = r.get("linked_constraint")
        if lc and lc not in keys:
            errs.append(f"strategic_risks[{i}] links unknown constraint {lc!r}")
    return errs


def validate(obj, snapshot: dict, constraints: list[dict]) -> list[str]:
    """Full validation: structure first; if that passes, grounding."""
    errs = validate_structure(obj)
    if errs:
        return errs
    return ground(obj, build_index(snapshot, constraints))


def drop_ungrounded(obj: dict, index: dict) -> int:
    """Remove COAs/constraints/risks that cite unknown keys/entities. Returns count dropped.

    The last-resort path: when repair retries are exhausted, nothing ungrounded is
    shown to leadership (doc 06 §4).
    """
    keys = index.get("constraint_keys", set())
    entities = index.get("entities", set())
    dropped = 0

    def _coa_ok(c):
        if c.get("constraint_key") and c["constraint_key"] not in keys:
            return False
        return all(ref in entities for ref in (c.get("entity_refs") or []))

    for section, ok in (
        ("operational_constraints", lambda c: not c.get("constraint_key") or c["constraint_key"] in keys),
        ("courses_of_action", _coa_ok),
        ("strategic_risks", lambda r: not r.get("linked_constraint") or r["linked_constraint"] in keys),
    ):
        items = obj.get(section, []) or []
        kept = [it for it in items if ok(it)]
        dropped += len(items) - len(kept)
        obj[section] = kept
    return dropped


def repair_hint(violations: list[str]) -> str:
    """Format violations as concise feedback for a repair re-prompt."""
    lines = "\n".join(f"- {v}" for v in violations[:20])
    return (
        "Your previous output was rejected. Fix exactly these problems and return the "
        "corrected JSON object only:\n" + lines
    )
