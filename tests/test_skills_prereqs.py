"""apps.skills.prereqs — the shared transitive prerequisite expander + dependency ordering."""
from __future__ import annotations

import pytest

from apps.sde.models import SdeCategory, SdeGroup, SdeType, SdeTypeSkill
from apps.skills.prereqs import expand_prerequisites, order_by_prereqs

pytestmark = pytest.mark.django_db

# type ids
A, B, C, X, Y = 7001, 7002, 7003, 7004, 7005


def _skill(type_id, name, prereqs=None):
    cat, _ = SdeCategory.objects.get_or_create(category_id=16, defaults={"name": "Skill"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=8001, defaults={"category": cat, "name": "S"})
    SdeType.objects.get_or_create(type_id=type_id, defaults={"name": name, "group": grp,
                                                             "published": True, "rank": 1})
    for psid, plvl in (prereqs or []):
        SdeTypeSkill.objects.get_or_create(type_id=type_id, skill_type_id=psid, defaults={"level": plvl})


def test_expand_pulls_in_transitive_prerequisites():
    # A needs B(3); B needs C(2). Asking for A(4) must surface the whole chain.
    _skill(C, "C")
    _skill(B, "B", prereqs=[(C, 2)])
    _skill(A, "A", prereqs=[(B, 3)])
    assert expand_prerequisites({A: 4}) == {A: 4, B: 3, C: 2}


def test_expand_takes_the_max_level_per_skill():
    # A needs B(2); the caller also wants B(4) directly → B ends at 4.
    _skill(B, "B")
    _skill(A, "A", prereqs=[(B, 2)])
    assert expand_prerequisites({A: 5, B: 4}) == {A: 5, B: 4}


def test_expand_is_bounded_on_a_cycle():
    # A ↔ B is not valid SDE, but the expander must terminate rather than loop.
    _skill(A, "A", prereqs=[(B, 3)])
    _skill(B, "B", prereqs=[(A, 3)])
    result = expand_prerequisites({A: 4})
    assert set(result) == {A, B}


def test_expand_clamps_levels_and_drops_zero():
    _skill(A, "A")
    assert expand_prerequisites({A: 9}) == {A: 5}
    assert expand_prerequisites({A: 0}) == {}


def test_order_places_prerequisites_before_dependents():
    _skill(C, "C")
    _skill(B, "B", prereqs=[(C, 2)])
    _skill(A, "A", prereqs=[(B, 3)])
    targets = expand_prerequisites({A: 4})
    order = [sid for sid, _ in order_by_prereqs(targets)]
    assert order.index(C) < order.index(B) < order.index(A)


def test_order_breaks_ties_by_training_cost():
    # Two independent skills: the cheaper one leads (quick wins first).
    _skill(X, "X")
    _skill(Y, "Y")
    cost = {X: 500_000, Y: 10_000}
    order = [sid for sid, _ in order_by_prereqs({X: 4, Y: 4}, sp_of=cost.get)]
    assert order == [Y, X]


def test_order_empty_is_empty():
    assert order_by_prereqs({}) == []
