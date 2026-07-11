"""Transitive skill-prerequisite expansion and dependency-ordered step sequencing.

General ``apps.sde`` utilities, deliberately free of any character/plan coupling so every planner
can share them: the doctrine plan generator here and the Capsuleer Path career plan builder both
call the same closure and ordering.

The problem they solve is the *prerequisite undercount*. A fit or template asks for, say,
"Logistics Cruisers IV", but to inject and train that skill EVE first requires its own prerequisite
skills (``Logistics V``, and so on). A plan built from the directly-required skills alone is
untrainable in order and understates the real work. :func:`expand_prerequisites` walks
``sde.SdeTypeSkill`` (each row: "to use ``type`` you need ``skill_type`` at ``level``", which for a
skill ``type`` is that skill's own prerequisite) to the full closure, taking the highest level
demanded for each skill. :func:`order_by_prereqs` then topologically orders the closure so every
prerequisite trains before the skill that needs it, breaking ties by training cost (quick wins
first) for a determinate, sensible sequence.

Both are read-only, bounded (a depth guard tolerates a pathological or cyclic SDE — real skill
prerequisites form a DAG), and clamp levels to the 1–5 EVE range.
"""
from __future__ import annotations

import heapq

from apps.sde.models import SdeTypeSkill

# Real EVE skill-prerequisite chains are only a handful deep; this bounds a cyclic/bad SDE so the
# closure can never loop forever (the DoS guard of doc 09 T-23).
_MAX_DEPTH = 25


def _clamp_level(level) -> int:
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 0
    return max(0, min(level, 5))


def expand_prerequisites(targets: dict[int, int]) -> dict[int, int]:
    """The full prerequisite closure of ``{skill_type_id: level}`` (max level per skill).

    Each target skill is pulled in at its requested level; every skill's own prerequisite skills
    (``SdeTypeSkill`` rows keyed by the skill as ``type``) are pulled in transitively at the highest
    level any path demands. Prerequisite level does not depend on how high you train the dependent,
    so each skill's prerequisites are expanded once. Batched one query per breadth wave; bounded by
    :data:`_MAX_DEPTH`. Levels are clamped to 1–5; a level of 0 is dropped.
    """
    result: dict[int, int] = {}
    frontier: dict[int, int] = {}
    for sid, level in targets.items():
        level = _clamp_level(level)
        if level >= 1:
            frontier[int(sid)] = max(frontier.get(int(sid), 0), level)

    expanded: set[int] = set()
    depth = 0
    while frontier and depth < _MAX_DEPTH:
        depth += 1
        for sid, level in frontier.items():
            result[sid] = max(result.get(sid, 0), level)
        to_expand = [sid for sid in frontier if sid not in expanded]
        expanded.update(to_expand)
        next_frontier: dict[int, int] = {}
        if to_expand:
            for row in SdeTypeSkill.objects.filter(type_id__in=to_expand):
                lvl = _clamp_level(row.level)
                if lvl >= 1:
                    next_frontier[row.skill_type_id] = max(next_frontier.get(row.skill_type_id, 0), lvl)
        # Only keep entries that still raise a skill's level — the cycle/stability guard.
        frontier = {s: lvl for s, lvl in next_frontier.items() if lvl > result.get(s, 0)}
    return result


def order_by_prereqs(targets: dict[int, int], sp_of=None) -> list[tuple[int, int]]:
    """Order ``{skill_type_id: level}`` so prerequisites come before dependents, quick wins first.

    A Kahn topological sort over the prerequisite edges *within* ``targets`` (edges to skills not in
    the set are ignored — those are assumed already trained or handled elsewhere), with a priority
    tie-break by ascending training cost via ``sp_of(skill_type_id) -> sp`` (then skill id, for
    determinism). Returns ``[(skill_type_id, level)]`` in trainable order. A residual cycle (should
    never occur with real SDE data) is appended by cost so the caller always gets every skill.
    """
    ids = {int(sid) for sid in targets}
    if not ids:
        return []
    cost = {sid: (sp_of(sid) if sp_of else 0) for sid in ids}

    prereqs: dict[int, set[int]] = {sid: set() for sid in ids}
    dependents: dict[int, list[int]] = {sid: [] for sid in ids}
    for row in SdeTypeSkill.objects.filter(type_id__in=ids):
        if row.skill_type_id in ids and row.skill_type_id != row.type_id:
            prereqs[row.type_id].add(row.skill_type_id)
    for sid, reqs in prereqs.items():
        for req in reqs:
            dependents[req].append(sid)

    indegree = {sid: len(prereqs[sid]) for sid in ids}
    ready = [(cost[sid], sid) for sid in ids if indegree[sid] == 0]
    heapq.heapify(ready)

    order: list[tuple[int, int]] = []
    placed: set[int] = set()
    while ready:
        _, sid = heapq.heappop(ready)
        order.append((sid, targets[sid]))
        placed.add(sid)
        for dep in dependents[sid]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                heapq.heappush(ready, (cost[dep], dep))

    if len(placed) < len(ids):
        # Residual cycle: append the remainder deterministically by cost.
        for sid in sorted(ids - placed, key=lambda s: (cost[s], s)):
            order.append((sid, targets[sid]))
    return order
