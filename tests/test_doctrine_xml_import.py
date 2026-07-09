"""EVE-client XML doctrine importer — parsing, classification, commit, security.

Covers the acceptance criteria for the XML import feature: a hardened parser
that rejects hostile files, honest duplicate/conflict classification, a preview
that never writes, a commit that applies only confirmed actions, and the
access-control / XSS / SQL-injection guarantees.

Uses types from the bundled test SDE (``sde`` fixture): Rifter 587,
200mm AutoCannon I 484, Damage Control I 2046, Fusion S 192, Test Cruiser 600.
"""
from __future__ import annotations

import pytest

from apps.doctrines import xml_import
from apps.doctrines import xml_parser as P
from apps.doctrines.models import Doctrine, DoctrineFit, DoctrineImportBatch
from apps.doctrines.services import create_fit
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

BASE = "/ops/admin/doctrines/import-xml/"


# --- helpers -----------------------------------------------------------------
def _user(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for role in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _xml(fittings) -> bytes:
    """Build a valid fittings export. ``fittings`` = list of
    ``(name, ship, [(slot, type, qty|None), …])``."""
    parts = ['<?xml version="1.0" ?>', "<fittings>"]
    for name, ship, hardware in fittings:
        parts.append(f'<fitting name="{name}">')
        parts.append(f'<shipType value="{ship}"/>')
        for slot, typ, qty in hardware:
            q = f' qty="{qty}"' if qty is not None else ""
            parts.append(f'<hardware slot="{slot}" type="{typ}"{q}/>')
        parts.append("</fitting>")
    parts.append("</fittings>")
    return "\n".join(parts).encode()


RIFTER_HW = [
    ("hi slot 0", "200mm AutoCannon I", 2),
    ("low slot 0", "Damage Control I", None),
    ("cargo", "Fusion S", 100),
]


def _classify(fittings):
    raw = P.parse_fittings_xml(_xml(fittings))
    return xml_import.classify_fittings(raw)


# ============================================================================
# Parser — safety (pure, no DB)
# ============================================================================
@pytest.mark.parametrize("payload,exc", [
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><fittings/>', P.ForbiddenConstructError),
    (b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://evil/x.dtd"><fittings/>', P.ForbiddenConstructError),
    (b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "x"><!ENTITY b "&a;&a;">]><fittings>&b;</fittings>',
     P.ForbiddenConstructError),
    (b'<foo><bar/></foo>', P.SchemaError),
    (b'<fittings><fitting name="x" evil="1"><shipType value="y"/></fitting></fittings>', P.SchemaError),
    (b'<fittings><fitting name="x"><shipType value="y"><deep/></shipType></fitting></fittings>', P.SchemaError),
    (b'<fittings><nope/></fittings>', P.SchemaError),
    (b'<?xml version="1.0"?><fittings><?php evil ?><fitting name="x"><shipType value="y"/></fitting></fittings>',
     P.ForbiddenConstructError),
    (b'\x00\x01\x02binary', P.NotXmlError),
    (b'', P.NotXmlError),
    (b'<fittings><fitting name="x"><shipType value="y"/></fittings>', P.MalformedXmlError),
])
def test_parser_rejects_hostile_input(payload, exc):
    with pytest.raises(exc):
        P.parse_fittings_xml(payload)


def _n_fittings(n: int) -> bytes:
    body = "".join(
        f'<fitting name="f{i}"><shipType value="Rifter"/></fitting>' for i in range(n)
    )
    return f"<fittings>{body}</fittings>".encode()


def test_parser_rejects_above_the_hard_ceiling():
    with pytest.raises(P.LimitExceededError):
        P.parse_fittings_xml(_n_fittings(P.MAX_FITTINGS_CEILING + 1))


def test_parser_honours_a_lower_configured_limit():
    # 5 fittings, limit 3 -> rejected.
    with pytest.raises(P.LimitExceededError):
        P.parse_fittings_xml(_n_fittings(5), max_fittings=3)
    # Same file under a generous limit parses fine.
    assert len(P.parse_fittings_xml(_n_fittings(5), max_fittings=10)) == 5


def test_configured_limit_is_clamped_to_the_ceiling():
    # A silly-high config can't lift the cap above the hard ceiling.
    assert P.clamp_max_fittings(10_000_000) == P.MAX_FITTINGS_CEILING
    assert P.clamp_max_fittings(0) == 1
    assert P.clamp_max_fittings(None) == P.DEFAULT_MAX_FITTINGS
    with pytest.raises(P.LimitExceededError):
        P.parse_fittings_xml(_n_fittings(P.MAX_FITTINGS_CEILING + 1), max_fittings=10_000_000)


def test_parser_rejects_too_many_hardware():
    hw = "".join(
        '<hardware slot="cargo" type="Fusion S" qty="1"/>'
        for _ in range(P.MAX_HARDWARE_PER_FIT + 1)
    )
    xml = f'<fittings><fitting name="big"><shipType value="Rifter"/>{hw}</fitting></fittings>'
    with pytest.raises(P.LimitExceededError):
        P.parse_fittings_xml(xml.encode())


def test_parser_missing_qty_defaults_to_one():
    raw = P.parse_fittings_xml(_xml([("A", "Rifter", [("cargo", "Fusion S", None)])]))
    assert raw[0].hardware[0].quantity == 1


def test_parser_normalises_all_slot_kinds():
    slots = [
        ("hi slot 3", "high", 3), ("med slot 1", "med", 1), ("low slot 2", "low", 2),
        ("rig slot 0", "rig", 0), ("subsystem slot 4", "subsystem", 4),
        ("drone bay", "drone", None), ("cargo", "cargo", None),
    ]
    hw = [(s, "Fusion S", 1) for s, _, _ in slots]
    raw = P.parse_fittings_xml(_xml([("A", "Rifter", hw)]))
    got = [(h.slot_category, h.slot_index) for h in raw[0].hardware]
    assert got == [(cat, idx) for _, cat, idx in slots]


@pytest.mark.parametrize("qty", ["0", "-3", "abc", str(P.MAX_QTY + 1)])
def test_parser_flags_bad_quantity_per_fitting(qty):
    raw = P.parse_fittings_xml(_xml([("A", "Rifter", [("cargo", "Fusion S", qty)])]))
    assert raw[0].errors  # this fitting is flagged, not a whole-file abort


# ============================================================================
# Classification (DB + SDE)
# ============================================================================
def test_happy_path_new_and_slots(sde):
    entries, counts = _classify([("Rifter Tackle", "Rifter", RIFTER_HW)])
    assert counts == {"new": 1, "total": 1}
    e = entries[0]
    assert e["status"] == xml_import.STATUS_NEW
    assert e["ship_type_id"] == 587
    # qty default applied; cargo + module slots kept
    dc = next(m for m in e["modules"] if m["type_id"] == 2046)
    assert dc["quantity"] == 1
    assert {m["slot"] for m in e["modules"]} >= {"High 0", "Low 0", "Cargo"}


def test_identical_existing_is_skipped(sde):
    _, _ = _classify([("Rifter Tackle", "Rifter", RIFTER_HW)])  # warms nothing; explicit below
    d = Doctrine.objects.create(name="Rifter Tackle")
    create_fit(d, name="Rifter Tackle", ship_type_id=587, modules=[
        {"type_id": 484, "quantity": 2}, {"type_id": 2046, "quantity": 1}, {"type_id": 192, "quantity": 100},
    ])
    entries, _ = _classify([("Rifter Tackle", "Rifter", RIFTER_HW)])
    assert entries[0]["status"] == xml_import.STATUS_IDENTICAL


def test_conflict_same_name_hull_different_fit(sde):
    d = Doctrine.objects.create(name="Rifter Tackle")
    create_fit(d, name="Rifter Tackle", ship_type_id=587,
               modules=[{"type_id": 484, "quantity": 1}])
    entries, _ = _classify([("Rifter Tackle", "Rifter", RIFTER_HW)])
    e = entries[0]
    assert e["status"] == xml_import.STATUS_CONFLICT
    assert e["existing"]["doctrine_id"] == d.id
    assert e["diff"]  # a human diff was produced


def test_possible_duplicate_fit_different_name(sde):
    d = Doctrine.objects.create(name="Something Else")
    create_fit(d, name="x", ship_type_id=587, modules=[
        {"type_id": 484, "quantity": 2}, {"type_id": 2046, "quantity": 1}, {"type_id": 192, "quantity": 100},
    ])
    entries, _ = _classify([("Brand New Name", "Rifter", RIFTER_HW)])
    e = entries[0]
    assert e["status"] == xml_import.STATUS_DUPLICATE_FIT
    assert e["warnings"]


def test_name_clash_different_hull(sde):
    d = Doctrine.objects.create(name="Rifter Tackle")
    create_fit(d, name="x", ship_type_id=600, modules=[])  # Test Cruiser hull
    entries, _ = _classify([("Rifter Tackle", "Rifter", RIFTER_HW)])
    assert entries[0]["status"] == xml_import.STATUS_HULL_CONFLICT


def test_invalid_unknown_ship_item_and_nonship(sde):
    entries, _ = _classify([
        ("Unknown Ship", "Frobnicator", [("cargo", "Fusion S", 1)]),
        ("Unknown Item", "Rifter", [("cargo", "Nonexistent Widget", 1)]),
        ("Not A Ship", "200mm AutoCannon I", [("cargo", "Fusion S", 1)]),
    ])
    assert [e["status"] for e in entries] == [xml_import.STATUS_INVALID] * 3
    assert any("not a ship" in r.lower() for r in entries[2]["reasons"])


# ============================================================================
# Commit (DB + SDE)
# ============================================================================
def _commit(user, fittings, decisions, filename="doctrines.xml"):
    entries, counts = _classify(fittings)
    batch = xml_import.create_batch(user, filename, 100, entries, counts)
    result = xml_import.commit_batch(batch, decisions, user)
    return batch, result


def test_commit_creates_new_doctrine_and_skills(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    _, result = _commit(user, [("Rifter Tackle", "Rifter", RIFTER_HW)], {"0": {"action": "import"}})
    assert result["created"] == 1
    fit = DoctrineFit.objects.get(doctrine__name="Rifter Tackle")
    assert fit.ship_type_id == 587
    assert fit.skill_requirements.exists()  # derived from dogma


def test_preview_does_not_write(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    entries, counts = _classify([("Rifter Tackle", "Rifter", RIFTER_HW)])
    xml_import.create_batch(user, "d.xml", 100, entries, counts)
    # Staging exists but no doctrine was created by classification/preview.
    assert DoctrineImportBatch.objects.count() == 1
    assert not Doctrine.objects.filter(name="Rifter Tackle").exists()


def test_commit_identical_makes_no_duplicate(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    d = Doctrine.objects.create(name="Rifter Tackle")
    create_fit(d, name="Rifter Tackle", ship_type_id=587, modules=[
        {"type_id": 484, "quantity": 2}, {"type_id": 2046, "quantity": 1}, {"type_id": 192, "quantity": 100},
    ])
    _, result = _commit(user, [("Rifter Tackle", "Rifter", RIFTER_HW)], {})
    assert result["identical"] == 1 and result["created"] == 0
    assert Doctrine.objects.filter(name="Rifter Tackle").count() == 1


def test_commit_conflict_skip_leaves_existing(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    d = Doctrine.objects.create(name="Rifter Tackle")
    fit = create_fit(d, name="orig", ship_type_id=587, modules=[{"type_id": 484, "quantity": 1}])
    _, result = _commit(user, [("Rifter Tackle", "Rifter", RIFTER_HW)], {"0": {"action": "skip"}})
    fit.refresh_from_db()
    assert result["skipped"] == 1
    assert fit.modules == [{"type_id": 484, "quantity": 1}]  # untouched


def test_commit_conflict_rename_creates_new(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    d = Doctrine.objects.create(name="Rifter Tackle")
    create_fit(d, name="orig", ship_type_id=587, modules=[{"type_id": 484, "quantity": 1}])
    _, result = _commit(user, [("Rifter Tackle", "Rifter", RIFTER_HW)],
                        {"0": {"action": "rename", "new_name": "Rifter Tackle Mk2"}})
    assert result["renamed"] == 1
    assert Doctrine.objects.filter(name="Rifter Tackle Mk2").exists()
    assert Doctrine.objects.filter(name="Rifter Tackle").count() == 1  # original still single


def test_commit_conflict_replace_updates_in_place(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    d = Doctrine.objects.create(name="Rifter Tackle")
    fit = create_fit(d, name="orig", ship_type_id=587, modules=[{"type_id": 484, "quantity": 1}])
    original_fit_id, original_doc_id = fit.id, d.id
    _, result = _commit(user, [("Rifter Tackle", "Rifter", RIFTER_HW)], {"0": {"action": "replace"}})
    assert result["replaced"] == 1
    fit.refresh_from_db()
    # Same rows preserved (no orphaned references), contents replaced.
    assert fit.id == original_fit_id and fit.doctrine_id == original_doc_id
    assert {m["type_id"] for m in fit.modules} == {484, 2046, 192}
    assert Doctrine.objects.filter(name="Rifter Tackle").count() == 1


def test_commit_invalid_is_rejected(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    _, result = _commit(user, [("Bad", "Rifter", [("cargo", "Nonexistent Widget", 1)])], {})
    assert result["rejected"] == 1 and result["created"] == 0


def test_commit_name_clash_import_anyway_creates_second_doctrine(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    d = Doctrine.objects.create(name="Rifter Tackle")
    create_fit(d, name="x", ship_type_id=600, modules=[])  # different hull (Test Cruiser)
    _, result = _commit(user, [("Rifter Tackle", "Rifter", RIFTER_HW)], {"0": {"action": "import"}})
    assert result["created"] == 1
    # Same name on a different hull is explicitly allowed by the model.
    assert Doctrine.objects.filter(name="Rifter Tackle").count() == 2


# ============================================================================
# Views — access control, CSRF, IDOR
# ============================================================================
def test_access_control(sde, client, django_user_model):
    assert client.get(BASE).status_code == 302  # anon -> login
    client.force_login(_user(django_user_model, "member", rbac.ROLE_MEMBER))
    assert client.get(BASE).status_code == 403
    client.force_login(_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    assert client.get(BASE).status_code == 403  # doctrine mgmt is officer, bulk import is director
    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    assert client.get(BASE).status_code == 200


def test_idor_batch_is_owner_scoped(sde, client, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile

    d1 = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    d2 = _user(django_user_model, "d2", rbac.ROLE_DIRECTOR)
    client.force_login(d1)
    up = SimpleUploadedFile("d.xml", _xml([("A", "Rifter", RIFTER_HW)]), content_type="text/xml")
    resp = client.post(BASE + "upload/", {"xml": up})
    loc = resp.headers["Location"]
    # A *different* director cannot see d1's staging batch.
    client.force_login(d2)
    assert client.get(loc).status_code == 404


def test_csrf_enforced_on_upload(sde, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    up = SimpleUploadedFile("d.xml", _xml([("A", "Rifter", RIFTER_HW)]), content_type="text/xml")
    assert csrf_client.post(BASE + "upload/", {"xml": up}).status_code == 403


def test_non_xml_upload_rejected(sde, client, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    up = SimpleUploadedFile("notes.txt", b"just text", content_type="text/plain")
    resp = client.post(BASE + "upload/", {"xml": up}, follow=True)
    assert b"Only .xml files" in resp.content
    assert DoctrineImportBatch.objects.count() == 0


def test_oversized_upload_rejected(sde, client, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    big = b"<fittings/>" + b" " * (P.MAX_FILE_BYTES + 1)
    up = SimpleUploadedFile("big.xml", big, content_type="text/xml")
    resp = client.post(BASE + "upload/", {"xml": up}, follow=True)
    assert b"larger than" in resp.content
    assert DoctrineImportBatch.objects.count() == 0


def test_binary_disguised_as_xml_rejected(sde, client, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    up = SimpleUploadedFile("evil.xml", b"\x00\x01\x02\x03PK\x03\x04", content_type="text/xml")
    resp = client.post(BASE + "upload/", {"xml": up}, follow=True)
    assert b"Import rejected" in resp.content
    assert DoctrineImportBatch.objects.count() == 0


# ============================================================================
# Injection / XSS
# ============================================================================
def test_fitting_name_xss_is_escaped_in_preview(sde, client, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    xml = (
        b'<?xml version="1.0"?><fittings>'
        b'<fitting name="&lt;script&gt;alert(1)&lt;/script&gt; Rifter">'
        b'<shipType value="Rifter"/>'
        b'<hardware slot="hi slot 0" type="200mm AutoCannon I"/></fitting></fittings>'
    )
    up = SimpleUploadedFile("x.xml", xml, content_type="text/xml")
    resp = client.post(BASE + "upload/", {"xml": up}, follow=True)
    html = resp.content
    assert b"<script>alert(1)" not in html          # never rendered as live markup
    assert b"&lt;script&gt;alert(1)" in html        # shown as text


def test_sql_injection_in_name_is_treated_as_text(sde, django_user_model):
    user = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    evil = "Rifter'); DROP TABLE doctrines_doctrine;--"
    _, result = _commit(user, [(evil, "Rifter", RIFTER_HW)], {"0": {"action": "import"}})
    assert result["created"] == 1
    # The table still exists (query works) and the literal name was stored safely.
    assert Doctrine.objects.filter(name=evil).exists()


def test_sql_injection_in_item_name_is_unresolved_not_executed(sde):
    evil_item = "x'; DROP TABLE sde_sdetype;--"
    entries, _ = _classify([("A", "Rifter", [("cargo", evil_item, 1)])])
    assert entries[0]["status"] == xml_import.STATUS_INVALID
    # SDE table intact.
    from apps.sde.models import SdeType
    assert SdeType.objects.filter(type_id=587).exists()


# ============================================================================
# Configurable per-import fitting limit
# ============================================================================
def test_config_default_is_the_ceiling(sde):
    from apps.doctrines.models import DoctrineImportConfig

    assert DoctrineImportConfig.active().effective_max_fittings() == P.MAX_FITTINGS_CEILING


def test_upload_honours_configured_limit(sde, client, django_user_model):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.doctrines.models import DoctrineImportConfig

    cfg = DoctrineImportConfig.active()
    cfg.max_fittings_per_import = 2
    cfg.save()
    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    xml = _xml([(f"F{i}", "Rifter", []) for i in range(3)])
    resp = client.post(
        BASE + "upload/",
        {"xml": SimpleUploadedFile("d.xml", xml, content_type="text/xml")},
        follow=True,
    )
    assert b"more than 2 fittings" in resp.content
    assert DoctrineImportBatch.objects.count() == 0


def test_settings_director_can_change_limit(sde, client, django_user_model):
    from apps.doctrines.models import DoctrineImportConfig

    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    assert client.get(BASE + "settings/").status_code == 200
    client.post(BASE + "settings/", {"max_fittings_per_import": "750"})
    assert DoctrineImportConfig.active().max_fittings_per_import == 750


def test_settings_clamps_above_ceiling(sde, client, django_user_model):
    from apps.doctrines.models import DoctrineImportConfig

    client.force_login(_user(django_user_model, "d1", rbac.ROLE_DIRECTOR))
    client.post(BASE + "settings/", {"max_fittings_per_import": "999999"})
    assert DoctrineImportConfig.active().max_fittings_per_import == P.MAX_FITTINGS_CEILING


def test_settings_blocks_non_directors(sde, client, django_user_model):
    client.force_login(_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    assert client.get(BASE + "settings/").status_code == 403
    assert client.post(BASE + "settings/", {"max_fittings_per_import": "1"}).status_code == 403


# ============================================================================
# Regression — existing ESI import entry points still present
# ============================================================================
def test_doctrines_admin_offers_both_import_methods(sde, client, django_user_model):
    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    html = client.get("/ops/admin/doctrines/").content
    assert b"Import my saved fits" in html          # ESI path preserved
    assert b"Import from EVE XML" in html            # new path added
