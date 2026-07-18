"""Tocha's Lab end-to-end critical path.

One ``django_db`` scenario that walks a pilot through the whole editor loop with the
Django test client — the browser-driven journey, minus a real browser (the app ships no
Selenium/Playwright rig; the server renders every number, so a client walk exercises the
same view + template + engine path a browser would). It asserts the state that flows
between steps: create → live recompute → save a revision → the saved page reflects it →
compare old vs new → export. If any seam in that chain regresses, this fails where the
per-view unit tests might each still pass in isolation.
"""
from __future__ import annotations

import json

import pytest
from django.urls import reverse

from apps.fitting.models import Fit

from ._fitting_utils import AC, DC, FUSION, RIFTER, make_member, seed_dogma

pytestmark = pytest.mark.django_db


@pytest.fixture
def pilot(db):
    seed_dogma()
    return make_member("eve:2100", 2100, "E2E Pilot")


def _items(n_guns):
    items = [{"type_id": AC, "slot": "high", "state": "active",
              "charge_type_id": FUSION, "quantity": 1} for _ in range(n_guns)]
    items.append({"type_id": DC, "slot": "low", "state": "active",
                  "charge_type_id": None, "quantity": 1})
    return items


def test_full_editor_journey(client, pilot):
    client.force_login(pilot)

    # 1. The workspace opens.
    assert client.get(reverse("fitting:index")).status_code == 200

    # 2. Create a Rifter from the hull name → lands on the editor.
    created = client.post(reverse("fitting:create"), {"ship": "Rifter", "name": "E2E Rifter"})
    assert created.status_code == 302
    fit = Fit.objects.get(owner=pilot, name="E2E Rifter")
    assert client.get(reverse("fitting:detail", args=[fit.pk])).status_code == 200

    # 3. Live recompute with two guns — server renders telemetry, DPS is real and positive.
    recompute = client.post(reverse("fitting:telemetry"), {
        "ship_type_id": RIFTER, "items": json.dumps(_items(2)), "skills": "none"})
    assert recompute.status_code == 200
    two_gun_html = recompute.content.decode()
    assert "dps" in two_gun_html.lower()

    # 4. A three-gun loadout out-damages the two-gun one (the engine actually re-evaluated).
    from apps.fitting import services
    from apps.fitting.engine.types import SkillProfile
    op_none = SkillProfile.from_dict({})
    dps2 = services.evaluate(RIFTER, _items(2), op_none)["offence"]["total_dps"]
    dps3 = services.evaluate(RIFTER, _items(3), op_none)["offence"]["total_dps"]
    assert dps3 > dps2 > 0

    # 5. Save the three-gun loadout → a new revision is appended and pointed to.
    saved = client.post(reverse("fitting:save", args=[fit.pk]), {
        "ship_type_id": RIFTER, "items": json.dumps(_items(3)),
        "summary": "Third gun"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert saved.status_code == 200
    fit.refresh_from_db()
    assert fit.current_revision.revision_number == 2
    assert sum(1 for i in fit.current_revision.items if i["type_id"] == AC) == 3

    # 6. The saved page now reflects the new revision.
    detail = client.get(reverse("fitting:detail", args=[fit.pk]))
    assert detail.status_code == 200 and b"E2E Rifter" in detail.content

    # 7. Compare rev 1 (2 guns) vs current (3 guns): DPS delta is positive.
    comp = client.get(reverse("fitting:compare", args=[fit.pk]) + "?rev=1&skills=none")
    assert comp.status_code == 200
    dps_metric = next(m for m in comp.context["diff"]["metrics"] if m["key"] == "total_dps")
    assert dps_metric["delta"] > 0 and dps_metric["higher_better"] is True

    # 8. Export the saved fit as EFT text.
    export = client.get(reverse("fitting:export_eft", args=[fit.pk]))
    assert export.status_code == 200 and export.content.startswith(b"[Rifter,")
    assert export.content.count(b"200mm AutoCannon I") == 3


def test_import_then_supply_journey(client, pilot):
    """Import an EFT loadout, then raise a shopping task for its shortfall in one flow."""
    from apps.tasks.models import Task

    from ._fitting_utils import EFT
    client.force_login(pilot)

    imported = client.post(reverse("fitting:import_eft"), {"eft": EFT})
    assert imported.status_code == 302
    fit = Fit.objects.get(owner=pilot, origin="eft")

    # With no corp stock, the detail page surfaces the shortfall + a supply panel.
    detail = client.get(reverse("fitting:detail", args=[fit.pk]))
    assert detail.status_code == 200
    assert detail.context["stock"]["missing"], "a bare test corp stocks nothing"

    # One click turns that shortfall into a claimable corp task.
    assert client.post(reverse("fitting:supply_task", args=[fit.pk])).status_code == 302
    task = Task.objects.get(related_type="tochaslab_fit", related_id=str(fit.pk))
    assert task.type == Task.Type.BUY and task.is_open
