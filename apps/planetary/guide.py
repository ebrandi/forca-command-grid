"""Per-planet setup guidance and the in-game build checklist (Journey 6).

Turns a plan's planets into practical, human guidance: what each planet is for, what
to extract or build, which facilities to place, what to route where, and what to
import/export. Marked clearly as guidance, not an in-game layout simulator.
"""
from __future__ import annotations

from .chains import PiGraph
from .constants import TIER_META
from .static_data import BUILD_CHECKLIST, COMMON_MISTAKES, ROLE_GUIDANCE


def planet_setup(plan, graph: PiGraph) -> list[dict]:
    """Structured setup guidance, one entry per planet in the plan."""
    out = []
    for planet in plan.planets.select_related("planet_type", "primary_material"):
        mat = planet.primary_material
        role = planet.role
        guidance = ROLE_GUIDANCE.get(role, ROLE_GUIDANCE["extract"])
        tier = graph.material(mat.type_id).tier if (mat and graph.material(mat.type_id)) else ""
        facility = TIER_META.get(tier, {}).get("facility", "")

        extract_from = imports = None
        if mat is not None:
            sch = graph.schematic_for(mat.type_id)
            if role == "extract":
                # The raw P0 leaves this planet must pull and refine.
                node = graph.requirements(mat.type_id, 1)
                leaves = graph.raw_leaves(node) if node else {}
                extract_from = [graph.material(t).name for t in leaves if graph.material(t)]
            elif sch:
                imports = [
                    {"name": graph.material(i).name if graph.material(i) else str(i),
                     "tier": graph.material(i).tier if graph.material(i) else "",
                     "quantity_per_cycle": q}
                    for i, q in sch.inputs.items()
                ]
        out.append({
            "planet_type": planet.planet_type.name,
            "planet_slug": planet.planet_type.slug,
            "role": role,
            "role_label": planet.get_role_display(),
            "product": mat.name if mat else None,
            "product_tier": tier,
            "facility": facility,
            "title": guidance["title"],
            "purpose": guidance["purpose"],
            "facilities": guidance["facilities"],
            "routes": guidance["routes"],
            "export": guidance["export"],
            "extract_from": extract_from,   # extraction planets
            "imports": imports,             # factory planets
        })
    return out


def common_mistakes() -> list[tuple[str, str]]:
    return COMMON_MISTAKES


def build_checklist() -> list[str]:
    return BUILD_CHECKLIST
