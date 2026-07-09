"""Production-chain traversal and the explorer queries (Journey 4).

Everything here is pure graph logic over the static PI rulebook — no prices, no ESI.
Load the graph once (``build_graph``) and reuse it across a request; all lookups are
in-memory so the chain explorer and the calculator never issue per-node queries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .constants import TIER_ORDER
from .models import PiMaterial, PiPlanetResource, PiPlanetType, PiSchematic


@dataclass(frozen=True)
class MaterialInfo:
    type_id: int
    name: str
    tier: str
    volume: float


@dataclass(frozen=True)
class SchematicInfo:
    schematic_id: int
    name: str
    output_id: int
    output_qty: int
    cycle_seconds: int
    tier: str
    inputs: dict[int, int]  # material type_id -> quantity per cycle


@dataclass
class ChainNode:
    """A node in a downward bill-of-materials for producing ``quantity`` of ``material``."""

    material: MaterialInfo
    quantity: Decimal
    schematic: SchematicInfo | None
    inputs: list[ChainNode] = field(default_factory=list)

    @property
    def is_raw(self) -> bool:
        return self.schematic is None


class PiGraph:
    """In-memory view of the PI rulebook."""

    def __init__(
        self,
        materials: dict[int, MaterialInfo],
        schematics: dict[int, SchematicInfo],
        planets_by_resource: dict[int, list[str]],
        resources_by_planet: dict[str, list[int]],
        planet_types: dict[str, PiPlanetType],
    ):
        self.materials = materials
        self.schem_by_output = schematics
        self.planets_by_resource = planets_by_resource
        self.resources_by_planet = resources_by_planet
        self.planet_types = planet_types
        # Reverse edge: material -> schematics that consume it.
        self.consumers: dict[int, list[SchematicInfo]] = {}
        for s in schematics.values():
            for mat_id in s.inputs:
                self.consumers.setdefault(mat_id, []).append(s)

    # -- lookups ----------------------------------------------------------- #
    def material(self, type_id: int) -> MaterialInfo | None:
        return self.materials.get(int(type_id))

    def schematic_for(self, type_id: int) -> SchematicInfo | None:
        return self.schem_by_output.get(int(type_id))

    # -- downward: what does producing this need? -------------------------- #
    def requirements(self, type_id: int, quantity=1, _seen=None) -> ChainNode | None:
        """Bill of materials to produce ``quantity`` of ``type_id``, to the P0 leaves."""
        type_id = int(type_id)
        mat = self.materials.get(type_id)
        if mat is None:
            return None
        _seen = _seen or set()
        qty = Decimal(str(quantity))
        schem = self.schem_by_output.get(type_id)
        if schem is None or type_id in _seen:  # raw resource (or cycle guard)
            return ChainNode(mat, qty, None)
        _seen = _seen | {type_id}
        runs = qty / Decimal(schem.output_qty) if schem.output_qty else Decimal(0)
        children = []
        for in_id, in_qty in schem.inputs.items():
            child = self.requirements(in_id, Decimal(in_qty) * runs, _seen)
            if child is not None:
                children.append(child)
        return ChainNode(mat, qty, schem, children)

    def raw_leaves(self, node: ChainNode) -> dict[int, Decimal]:
        """Aggregate the P0 (raw) requirement of a chain node."""
        out: dict[int, Decimal] = {}
        stack = [node]
        while stack:
            n = stack.pop()
            if n.is_raw:
                out[n.material.type_id] = out.get(n.material.type_id, Decimal(0)) + n.quantity
            else:
                stack.extend(n.inputs)
        return out

    def all_inputs_by_tier(self, node: ChainNode) -> dict[str, dict[int, Decimal]]:
        """Every input (any tier below the target) aggregated by tier."""
        out: dict[str, dict[int, Decimal]] = {t: {} for t in TIER_ORDER}
        stack = list(node.inputs)
        while stack:
            n = stack.pop()
            bucket = out.setdefault(n.material.tier, {})
            bucket[n.material.type_id] = bucket.get(n.material.type_id, Decimal(0)) + n.quantity
            stack.extend(n.inputs)
        return {t: v for t, v in out.items() if v}

    # -- planets required -------------------------------------------------- #
    def planets_for_resources(self, p0_type_ids) -> dict[int, list[str]]:
        """For each P0 type, which planet-type slugs can extract it."""
        return {int(t): self.planets_by_resource.get(int(t), []) for t in p0_type_ids}

    def planet_cover(self, p0_type_ids) -> list[str]:
        """A small set of planet-type slugs that together yield all the given P0s.

        Greedy set cover — good enough for "you need these planets" guidance.
        """
        needed = {int(t) for t in p0_type_ids if self.planets_by_resource.get(int(t))}
        chosen: list[str] = []
        while needed:
            best, best_hit = None, set()
            for slug, res in self.resources_by_planet.items():
                hit = needed & set(res)
                if len(hit) > len(best_hit):
                    best, best_hit = slug, hit
            if not best:
                break
            chosen.append(best)
            needed -= best_hit
        return chosen

    # -- upward: what can this become? ------------------------------------- #
    def becomes(self, type_id: int) -> list[SchematicInfo]:
        """Schematics that consume ``type_id`` (its immediate next-tier products)."""
        return list(self.consumers.get(int(type_id), []))

    def reachable_products(self, type_ids) -> list[MaterialInfo]:
        """Every product reachable by repeatedly consuming the given materials.

        Used for "given these planets/resources, what can I ultimately make?".
        A schematic is craftable once *all* its inputs are reachable.
        """
        have = {int(t) for t in type_ids}
        changed = True
        while changed:
            changed = False
            for s in self.schem_by_output.values():
                if s.output_id not in have and all(i in have for i in s.inputs):
                    have.add(s.output_id)
                    changed = True
        # Return the newly-craftable ones (exclude the raw inputs we started with).
        start = {int(t) for t in type_ids}
        return [self.materials[t] for t in sorted(have - start) if t in self.materials]

    def missing_inputs(self, type_id: int, available_type_ids) -> list[MaterialInfo]:
        """Which raw P0 leaves for producing ``type_id`` are NOT available."""
        node = self.requirements(type_id, 1)
        if node is None:
            return []
        have = {int(t) for t in available_type_ids}
        return [self.materials[t] for t in self.raw_leaves(node) if t not in have and t in self.materials]


def build_graph() -> PiGraph:
    """Load the whole PI rulebook into memory (a handful of queries)."""
    materials = {
        m.type_id: MaterialInfo(m.type_id, m.name, m.tier, m.volume)
        for m in PiMaterial.objects.all()
    }
    schematics: dict[int, SchematicInfo] = {}
    for s in PiSchematic.objects.prefetch_related("inputs"):
        schematics[s.output_id] = SchematicInfo(
            s.schematic_id, s.name, s.output_id, s.output_quantity, s.cycle_seconds, s.tier,
            {i.material_id: i.quantity for i in s.inputs.all()},
        )
    planets_by_resource: dict[int, list[str]] = {}
    resources_by_planet: dict[str, list[int]] = {}
    for pr in PiPlanetResource.objects.select_related("planet_type", "material"):
        planets_by_resource.setdefault(pr.material_id, []).append(pr.planet_type.slug)
        resources_by_planet.setdefault(pr.planet_type.slug, []).append(pr.material_id)
    planet_types = {p.slug: p for p in PiPlanetType.objects.all()}
    return PiGraph(materials, schematics, planets_by_resource, resources_by_planet, planet_types)
