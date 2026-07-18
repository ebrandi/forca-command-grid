"""Tocha's Lab feature tests: services, views, import/export, sharing, and security.

Uses a small self-contained fixture built from real EVE type ids (so EFT name resolution
works) — original data, nothing copied from an external fit library.
"""
from __future__ import annotations

import json

import pytest
from django.urls import reverse

from apps.fitting import services
from apps.fitting.engine.types import SkillProfile
from apps.fitting.models import Fit

from ._fitting_utils import AC, DC, EFT, FUSION, RIFTER, make_member, seed_dogma


@pytest.fixture
def dogma(db):
    return seed_dogma()


@pytest.fixture
def owner(dogma):
    return make_member("eve:2001", 2001, "Owner Pilot")


@pytest.fixture
def other(dogma):
    return make_member("eve:2002", 2002, "Other Pilot")


@pytest.fixture
def officer(dogma):
    from core import rbac
    return make_member("eve:2003", 2003, "Officer Pilot", role=rbac.ROLE_OFFICER)


# --------------------------------------------------------------------------- #
# Services: import/export
# --------------------------------------------------------------------------- #
def test_import_eft_preserves_charge_and_infers_slots(dogma):
    parsed = services.import_eft(EFT)
    assert parsed["ship_type_id"] == RIFTER
    guns = [i for i in parsed["items"] if i["type_id"] == AC]
    assert len(guns) == 2
    assert all(g["slot"] == "high" for g in guns)          # inferred from hiPower effect
    assert all(g["charge_type_id"] == FUSION for g in guns)  # charge preserved (unlike lossy parser)
    dc = [i for i in parsed["items"] if i["type_id"] == DC][0]
    assert dc["slot"] == "low"                              # inferred from loPower effect


def test_import_eft_reports_unresolved(dogma):
    parsed = services.import_eft("[Rifter, X]\nNonexistent Module 9000")
    assert "Nonexistent Module 9000" in parsed["unresolved"]


def test_export_eft_is_deterministic(dogma):
    parsed = services.import_eft(EFT)
    a = services.export_eft(RIFTER, parsed["items"], "Test Rifter")
    b = services.export_eft(RIFTER, parsed["items"], "Test Rifter")
    assert a == b
    assert a.startswith("[Rifter, Test Rifter]")
    assert "200mm AutoCannon I, Fusion S" in a


def test_import_eft_rejects_non_eft(dogma):
    with pytest.raises(ValueError):
        services.import_eft("just some text, not a fit")


# --------------------------------------------------------------------------- #
# Services: persistence + pricing + compare
# --------------------------------------------------------------------------- #
def test_create_save_and_fork(owner, dogma):
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R1", ship_type_id=RIFTER, items=parsed["items"])
    assert fit.current_revision.revision_number == 1
    services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1], user=owner)
    fit.refresh_from_db()
    assert fit.current_revision.revision_number == 2
    assert fit.revisions.count() == 2  # history is append-only
    fork = services.fork_fit(fit, fit.current_revision, owner)
    assert fork.forked_from_id == fit.pk and fork.origin == "fork"


def test_price_fit_uses_market_authority(owner, dogma):
    parsed = services.import_eft(EFT)
    priced = services.price_fit(RIFTER, parsed["items"])
    assert priced["total"] > 0            # 1000 ISK per seeded type
    assert priced["as_of"] is not None


def test_compare_shows_deltas(owner, dogma):
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])
    r2 = services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1], user=owner)
    diff = services.compare(fit.revisions.get(revision_number=1), r2, SkillProfile.omniscient())
    dps = next(m for m in diff["metrics"] if m["key"] == "total_dps")
    assert dps["delta"] < 0  # removing a gun lowers DPS
    assert diff["removed"]


# --------------------------------------------------------------------------- #
# Views + security
# --------------------------------------------------------------------------- #
def test_index_and_create_flow(client, owner, dogma):
    client.force_login(owner)
    assert client.get(reverse("fitting:index")).status_code == 200
    resp = client.post(reverse("fitting:create"), {"ship": "Rifter", "name": "My Rifter"})
    assert resp.status_code == 302
    fit = Fit.objects.get(owner=owner)
    detail = client.get(reverse("fitting:detail", args=[fit.pk]))
    assert detail.status_code == 200
    assert b"Tocha" in detail.content


def test_import_eft_view_creates_fit(client, owner, dogma):
    client.force_login(owner)
    resp = client.post(reverse("fitting:import_eft"), {"eft": EFT})
    assert resp.status_code == 302
    fit = Fit.objects.get(owner=owner)
    assert fit.ship_type_id == RIFTER
    assert fit.current_revision.items
    # the detail page renders REAL computed telemetry, not just labels (regression: the
    # engine's nested telemetry must be flattened for the template).
    detail = client.get(reverse("fitting:detail", args=[fit.pk]) + "?skills=none")
    assert detail.status_code == 200
    assert b"450" in detail.content  # Rifter base shield HP (untrained, no Shield Management)


def test_telemetry_endpoint_renders_server_side(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    resp = client.post(reverse("fitting:telemetry"), {
        "ship_type_id": RIFTER, "items": json.dumps(parsed["items"]), "skills": "allv"})
    assert resp.status_code == 200
    assert b"DPS" in resp.content and b"EHP" in resp.content


def test_telemetry_rejects_oversized_payload(client, owner, dogma):
    client.force_login(owner)
    huge = json.dumps([{"type_id": AC} for _ in range(400)])  # over the 300 item cap
    resp = client.post(reverse("fitting:telemetry"), {"ship_type_id": RIFTER, "items": huge})
    assert resp.status_code == 400


def test_share_link_lifecycle(client, owner, other, dogma):
    client.force_login(owner)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=[])
    client.post(reverse("fitting:share", args=[fit.pk]))
    fit.refresh_from_db()
    assert fit.share_token and fit.public_link_active
    # another pilot can open the public link by token
    client.force_login(other)
    assert client.get(reverse("fitting:shared", args=[fit.share_token])).status_code == 200
    # owner revokes -> token 404s
    client.force_login(owner)
    client.post(reverse("fitting:unshare", args=[fit.pk]))
    assert client.get(reverse("fitting:shared", args=[fit.share_token])).status_code == 404


def test_idor_private_fit_not_viewable(client, owner, other, dogma):
    fit = services.create_fit(owner, name="secret", ship_type_id=RIFTER, items=[])
    client.force_login(other)
    assert client.get(reverse("fitting:detail", args=[fit.pk])).status_code == 404
    # and cannot save into it
    assert client.post(reverse("fitting:save", args=[fit.pk]),
                       {"items": "[]", "ship_type_id": RIFTER}).status_code == 404


def test_manage_rename_duplicate_archive_restore_delete(client, owner, dogma):
    client.force_login(owner)
    fit = services.create_fit(owner, name="Orig", ship_type_id=RIFTER, items=[])
    client.post(reverse("fitting:rename", args=[fit.pk]), {"name": "Renamed"})
    fit.refresh_from_db()
    assert fit.name == "Renamed"
    client.post(reverse("fitting:duplicate", args=[fit.pk]))
    assert Fit.objects.filter(owner=owner, origin="duplicate").exists()
    client.post(reverse("fitting:archive", args=[fit.pk]))
    fit.refresh_from_db()
    assert fit.is_archived
    client.post(reverse("fitting:restore", args=[fit.pk]))
    fit.refresh_from_db()
    assert not fit.is_archived
    client.post(reverse("fitting:delete", args=[fit.pk]))
    assert not Fit.objects.filter(pk=fit.pk).exists()  # hard delete


def test_restore_revision_appends_a_new_revision(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])   # rev1
    services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1], user=owner)  # rev2
    fit.refresh_from_db()
    assert len(fit.current_revision.items) == 1
    client.post(reverse("fitting:restore_revision", args=[fit.pk, 1]))
    fit.refresh_from_db()
    assert fit.current_revision.revision_number == 3                       # append-only history
    assert len(fit.current_revision.items) == len(parsed["items"])         # rev3 content == rev1


def test_management_is_owner_only(client, owner, other, dogma):
    fit = services.create_fit(owner, name="Secret", ship_type_id=RIFTER, items=[])
    client.force_login(other)
    assert client.post(reverse("fitting:rename", args=[fit.pk]), {"name": "x"}).status_code == 404
    assert client.post(reverse("fitting:delete", args=[fit.pk])).status_code == 404
    assert Fit.objects.filter(pk=fit.pk).exists()


def test_load_doctrine_into_simulator(client, owner, dogma):
    from apps.doctrines.models import Doctrine, DoctrineFit
    doc = Doctrine.objects.create(name="Test Doctrine", status=Doctrine.Status.ACTIVE)
    dfit = DoctrineFit.objects.create(
        doctrine=doc, name="Rifter Tackle", ship_type_id=RIFTER,
        modules=[{"type_id": AC, "quantity": 3, "slot": "high"}])
    client.force_login(owner)
    # the Lab home points a member at the doctrine picker
    assert b"Browse doctrines" in client.get(reverse("fitting:index")).content
    # the picker lists the doctrine and its fit
    page = client.get(reverse("fitting:load_doctrine"))
    assert page.status_code == 200
    assert b"Test Doctrine" in page.content and b"Rifter Tackle" in page.content
    # loading a fit opens it in the simulator
    resp = client.post(reverse("fitting:import_doctrine", args=[dfit.pk]))
    assert resp.status_code == 302
    loaded = Fit.objects.filter(owner=owner, origin="doctrine").first()
    assert loaded and loaded.ship_type_id == RIFTER
    assert any(it["type_id"] == AC for it in loaded.current_revision.items)


def test_load_doctrine_paginates_and_filters(client, owner, dogma):
    """Every active doctrine is reachable via pagination + search — no arbitrary cap."""
    from apps.doctrines.models import Doctrine, DoctrineDisplayConfig, DoctrineFit
    cfg = DoctrineDisplayConfig.active()
    cfg.per_page = 6           # the floor; 8 doctrines then span 2 pages
    cfg.save()
    for i in range(8):
        d = Doctrine.objects.create(name=f"Doctrine {i:02d}", status=Doctrine.Status.ACTIVE)
        DoctrineFit.objects.create(doctrine=d, name=f"Fit {i:02d}", ship_type_id=RIFTER,
                                   modules=[{"type_id": AC, "quantity": 1, "slot": "high"}])
    client.force_login(owner)

    p1 = client.get(reverse("fitting:load_doctrine"))
    assert p1.status_code == 200
    assert p1.context["page_obj"].paginator.num_pages == 2
    assert len(p1.context["rows"]) == 6 and p1.context["total_all"] == 8

    p2 = client.get(reverse("fitting:load_doctrine") + "?page=2")
    assert len(p2.context["rows"]) == 2

    narrowed = client.get(reverse("fitting:load_doctrine"), {"q": "Doctrine 03"})
    assert narrowed.context["total_shown"] == 1 and b"Doctrine 03" in narrowed.content

    # an htmx filter request returns only the results fragment, not the whole page
    frag = client.get(reverse("fitting:load_doctrine"), HTTP_HX_REQUEST="true")
    assert b'id="dc-results"' in frag.content and b"<html" not in frag.content.lower()


def test_shared_view_requires_valid_token(client, owner, dogma):
    services.create_fit(owner, name="R", ship_type_id=RIFTER, items=[])  # exists but never shared
    # a guessed token resolves nothing
    assert client.get(reverse("fitting:shared", args=["deadbeefdeadbeef"])).status_code == 404


def test_export_eft_view(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])
    resp = client.get(reverse("fitting:export_eft", args=[fit.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    assert resp.content.startswith(b"[Rifter,")


def test_brand_localised_to_pt_br():
    """pt-BR must render the mandated 'Laboratório do Tocha'; other locales keep the
    brand 'Tocha's Lab' (English fallback). 'Tocha' is a proper name, never translated."""
    import re
    from pathlib import Path

    from django.conf import settings
    po = Path(settings.BASE_DIR) / "locale" / "pt_BR" / "LC_MESSAGES" / "django.po"
    text = po.read_text(encoding="utf-8")
    m = re.search(r'msgid "Tocha\'s Lab"\s*\nmsgstr "([^"]*)"', text)
    assert m and m.group(1) == "Laboratório do Tocha"


def test_training_export(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])
    resp = client.get(reverse("fitting:training_export", args=[fit.pk]) + "?skills=none")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    body = resp.content.decode()
    # untrained pilot -> the fit's required skills appear as an EVE skill-planner paste
    assert "Gunnery 1" in body or "Minmatar Frigate 1" in body


def test_search_endpoints(client, owner, dogma):
    client.force_login(owner)
    hulls = client.get(reverse("fitting:search_hulls"), {"q": "Rif"})
    assert hulls.status_code == 200 and any(r["type_id"] == RIFTER for r in hulls.json()["results"])
    mods = client.get(reverse("fitting:search_modules"), {"q": "AutoCannon"})
    assert any(r["type_id"] == AC for r in mods.json()["results"])


def test_telemetry_endpoint_accepts_a_target_profile(client, owner, dogma):
    """A target profile posted to the live recompute reaches the engine: an all-turret fit
    measured against a target honestly flags that turret application is not modelled."""
    client.force_login(owner)
    items = [{"type_id": AC, "slot": "high", "state": "active",
              "charge_type_id": FUSION, "quantity": 1}]
    resp = client.post(reverse("fitting:telemetry"), {
        "ship_type_id": RIFTER, "items": json.dumps(items), "skills": "none",
        "tgt_sig": "40", "tgt_vel": "300"})
    assert resp.status_code == 200
    assert b"turret_application_not_modelled" in resp.content


# --------------------------------------------------------------------------- #
# Supply / industry actions
# --------------------------------------------------------------------------- #
def _rifter_fit(owner):
    parsed = services.import_eft(EFT)
    return services.create_fit(owner, name="Supply Rifter", ship_type_id=RIFTER,
                               items=parsed["items"])


def test_shortfall_reads_the_stock_overlay(owner, dogma):
    """With no corp stock, every fit component is short — the same list the page shows."""
    from apps.fitting import supply
    fit = _rifter_fit(owner)
    short = supply.fit_shortfall(fit.ship_type_id, fit.current_revision.items)
    type_ids = {m["type_id"] for m in short}
    assert RIFTER in type_ids and AC in type_ids            # hull + modules are short
    assert all(m["short"] > 0 for m in short)


def test_supply_task_is_member_action_and_idempotent(client, owner, dogma):
    from apps.tasks.models import Task
    fit = _rifter_fit(owner)
    client.force_login(owner)
    resp = client.post(reverse("fitting:supply_task", args=[fit.pk]))
    assert resp.status_code == 302
    task = Task.objects.get(related_type="tochaslab_fit", related_id=str(fit.pk))
    assert task.type == Task.Type.BUY and task.is_open
    # a repeat click dedupes to the same open task (no spam)
    client.post(reverse("fitting:supply_task", args=[fit.pk]))
    assert Task.objects.filter(related_type="tochaslab_fit", related_id=str(fit.pk)).count() == 1


def test_supply_project_creates_draft_with_shortfall_items(client, owner, dogma):
    from apps.industry.models import IndustryProject
    fit = _rifter_fit(owner)
    client.force_login(owner)
    resp = client.post(reverse("fitting:supply_project", args=[fit.pk]))
    assert resp.status_code == 302
    project = IndustryProject.objects.filter(source=IndustryProject.Source.MANUAL).latest("pk")
    assert project.status == IndustryProject.Status.DRAFT
    item_type_ids = set(project.items.values_list("type_id", flat=True))
    assert RIFTER in item_type_ids and AC in item_type_ids


def test_supply_po_requires_officer_and_supplier(client, owner, officer, dogma):
    from apps.procurement.models import PurchaseOrder, Supplier

    # A plain member cannot draft a PO even for their own fit → 403 (viewable, wrong role).
    fit = _rifter_fit(owner)
    client.force_login(owner)
    assert client.post(reverse("fitting:supply_po", args=[fit.pk]),
                       {"supplier": 1}).status_code == 403

    # The officer acts on a fit they own — a viewer can only supply a fit they can see.
    off_fit = _rifter_fit(officer)
    client.force_login(officer)
    # No/invalid supplier → bounced back with an error, no PO minted.
    assert client.post(reverse("fitting:supply_po", args=[off_fit.pk])).status_code == 302
    assert PurchaseOrder.objects.count() == 0

    # With an active supplier, a DRAFT PO is drafted for the shortfall.
    supplier = Supplier.objects.create(kind=Supplier.Kind.HUB, display_name="Jita Seller",
                                       status=Supplier.Status.ACTIVE)
    resp = client.post(reverse("fitting:supply_po", args=[off_fit.pk]), {"supplier": supplier.pk})
    assert resp.status_code == 302
    po = PurchaseOrder.objects.get()
    assert po.supplier_id == supplier.pk and po.status == PurchaseOrder.Status.DRAFT
    assert {ln.type_id for ln in po.lines.all()} >= {RIFTER, AC}


def test_supply_action_needs_view_access(client, owner, other, dogma):
    """A member who cannot even view a private fit gets 404 from a supply action."""
    fit = _rifter_fit(owner)
    client.force_login(other)
    assert client.post(reverse("fitting:supply_task", args=[fit.pk])).status_code == 404
    assert client.post(reverse("fitting:supply_project", args=[fit.pk])).status_code == 404
