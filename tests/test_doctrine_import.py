"""Importing doctrine fits: ESI saved fits, and killmail → doctrine."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.admin_audit import console
from apps.doctrines.killmail_import import eft_from_killmail
from apps.doctrines.models import Doctrine
from apps.doctrines.services import create_fit
from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail, KillmailItem
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

RIFTER, AUTOCANNON, FUSION = 587, 484, 192


def _user(django_user_model, name, cid, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    # is_corp_director: since LP-4 the app's Director role is only exercisable from a pilot who
    # holds the in-game Director role, so a director fixture needs the seat that proves it.
    EveCharacter.objects.create(character_id=cid, user=user, name=name, is_main=True,
                                is_corp_member=True,
                                is_corp_director=rbac.ROLE_DIRECTOR in roles)
    return user


def _loss_with_fit(km_id=920001):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_time="2026-06-26T00:00:00Z",
        solar_system_id=30000142, victim_ship_type_id=RIFTER,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )
    KillmailItem.objects.create(killmail=km, idx=0, item_type_id=AUTOCANNON, flag=27,
                                quantity_destroyed=2)
    # Cargo ammo — must end up in the doctrine (a consumable the fit needs).
    KillmailItem.objects.create(killmail=km, idx=1, item_type_id=FUSION, flag=5,
                                quantity_dropped=100)
    return km


# --- helpers -----------------------------------------------------------------
@pytest.mark.django_db
def test_eft_from_killmail_includes_fit_and_cargo(sde):
    eft = eft_from_killmail(_loss_with_fit())
    assert eft.startswith("[Rifter, Rifter]")
    assert "200mm AutoCannon I x2" in eft
    assert "Fusion S x100" in eft  # cargo consumable carried into the doctrine


@pytest.mark.django_db
def test_create_fit_derives_skills_and_eft(sde):
    doctrine = Doctrine.objects.create(name="Test")
    fit = create_fit(doctrine, name="Rifter", ship_type_id=RIFTER,
                     modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}])
    assert fit.skill_requirements.count() >= 1
    assert fit.eft_text.startswith("[Rifter, Rifter]")


def test_fittings_scope_is_in_settings_and_catalog():
    from django.conf import settings as dj

    from apps.sso import scopes
    assert "fittings" in dj.EVE_SSO_FEATURE_SCOPES
    assert "fittings" in scopes.FEATURES_BY_KEY


# --- import from ESI saved fits ----------------------------------------------
@pytest.mark.django_db
def test_import_fits_prompts_to_grant_scope(client, django_user_model, sde, monkeypatch):
    client.force_login(_user(django_user_model, "dir", 7101, rbac.ROLE_DIRECTOR))
    monkeypatch.setattr(console.esi_fits, "characters_with_fittings_scope", lambda user: [])
    html = client.get(reverse("admin_audit:import_fits")).content.decode()
    assert "Grant fittings access" in html
    assert "?feature=fittings" in html


@pytest.mark.django_db
def test_import_fits_lists_saved_fits(client, django_user_model, sde, monkeypatch):
    client.force_login(_user(django_user_model, "dir", 7102, rbac.ROLE_DIRECTOR))
    fake = [{
        "fitting_id": 5, "name": "My Rifter", "ship_type_id": RIFTER, "ship_name": "Rifter",
        "modules": [{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
        "item_count": 2, "character_id": 7102, "character_name": "dir",
    }]
    monkeypatch.setattr(console.esi_fits, "characters_with_fittings_scope", lambda user: [object()])
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings", lambda user: fake)
    html = client.get(reverse("admin_audit:import_fits")).content.decode()
    assert "My Rifter" in html
    assert 'value="7102:5"' in html


@pytest.mark.django_db
def _fake_fit(fitting_id, cid, name="Saved name"):
    return {
        "fitting_id": fitting_id, "name": name, "ship_type_id": RIFTER, "ship_name": "Rifter",
        "modules": [{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
        "item_count": 2, "character_id": cid, "character_name": "dir",
    }


@pytest.mark.django_db
def test_each_fit_imports_as_its_own_doctrine_in_imported_category(client, django_user_model, sde, monkeypatch):
    from apps.doctrines.models import DoctrineCategory
    DoctrineCategory.objects.get_or_create(key="imported", defaults={"label": "IMPORTED"})
    client.force_login(_user(django_user_model, "dir", 7103, rbac.ROLE_DIRECTOR))
    fake = [_fake_fit(5, 7103, "Alpha"), _fake_fit(6, 7103, "Bravo")]
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings", lambda user: fake)
    resp = client.post(reverse("admin_audit:import_fits_apply"), {"select": ["7103:5", "7103:6"]})
    assert resp.status_code == 302
    # Two separate doctrines, both filed under IMPORTED.
    alpha = Doctrine.objects.get(name="Alpha")
    bravo = Doctrine.objects.get(name="Bravo")
    assert alpha.category.key == "imported" and bravo.category.key == "imported"
    assert alpha.fits.get().ship_type_id == RIFTER
    assert alpha.fits.get().skill_requirements.count() >= 1


@pytest.mark.django_db
def test_rename_is_optional_blank_keeps_saved_name(client, django_user_model, sde, monkeypatch):
    client.force_login(_user(django_user_model, "dir", 7110, rbac.ROLE_DIRECTOR))
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings",
                        lambda user: [_fake_fit(9, 7110, "My Saved Rifter")])
    # No name:7110:9 field at all → uses the saved fit name as the doctrine name.
    client.post(reverse("admin_audit:import_fits_apply"), {"select": ["7110:9"]})
    assert Doctrine.objects.filter(name="My Saved Rifter").exists()


@pytest.mark.django_db
def test_optional_rename_overrides_doctrine_name(client, django_user_model, sde, monkeypatch):
    client.force_login(_user(django_user_model, "dir", 7111, rbac.ROLE_DIRECTOR))
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings",
                        lambda user: [_fake_fit(9, 7111, "Saved")])
    client.post(reverse("admin_audit:import_fits_apply"),
                {"select": ["7111:9"], "name:7111:9": "Renamed Doctrine"})
    d = Doctrine.objects.get(name="Renamed Doctrine")
    assert d.category.key == "imported"
    # The fit inside keeps the saved name; the doctrine carries the rename.
    assert d.fits.get().name == "Saved"


@pytest.mark.django_db
def test_identical_reimport_is_skipped_not_duplicated(client, django_user_model, sde, monkeypatch):
    client.force_login(_user(django_user_model, "dir", 7120, rbac.ROLE_DIRECTOR))
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings",
                        lambda user: [_fake_fit(9, 7120, "Brawl Rifter")])
    # First import creates it.
    client.post(reverse("admin_audit:import_fits_apply"), {"select": ["7120:9"]})
    assert Doctrine.objects.filter(name="Brawl Rifter").count() == 1
    # Re-import the identical fit → no second doctrine.
    client.post(reverse("admin_audit:import_fits_apply"), {"select": ["7120:9"]})
    assert Doctrine.objects.filter(name="Brawl Rifter").count() == 1


@pytest.mark.django_db
def test_same_name_different_fit_is_a_conflict_not_imported(client, django_user_model, sde, monkeypatch):
    client.force_login(_user(django_user_model, "dir", 7121, rbac.ROLE_DIRECTOR))
    # Existing doctrine named "Brawl" with one fit.
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings",
                        lambda user: [_fake_fit(9, 7121, "Brawl")])
    client.post(reverse("admin_audit:import_fits_apply"), {"select": ["7121:9"]})
    assert Doctrine.objects.filter(name="Brawl").count() == 1
    # Now a *different* fit (extra module) but the same name → conflict, not created.
    diff = _fake_fit(10, 7121, "Brawl")
    diff["modules"] = diff["modules"] + [{"type_id": 192, "quantity": 100, "name": "Fusion S"}]
    monkeypatch.setattr(console.esi_fits, "fetch_all_fittings", lambda user: [diff])
    resp = client.post(reverse("admin_audit:import_fits_apply"), {"select": ["7121:10"]}, follow=True)
    assert Doctrine.objects.filter(name="Brawl").count() == 1  # still one — not duplicated
    body = resp.content.decode()
    assert "rename it and import again" in body.lower()


@pytest.mark.django_db
def test_name_conflict_helper_classifies_correctly(sde):
    from apps.doctrines.services import create_fit, name_conflict
    d = Doctrine.objects.create(name="Hawk Brawl")
    create_fit(d, name="Hawk", ship_type_id=RIFTER,
               modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "gun"}])
    # Identical → duplicate.
    kind, _ = name_conflict("hawk brawl", RIFTER, [{"type_id": AUTOCANNON, "quantity": 2}])
    assert kind == "duplicate"
    # Same name, different modules → conflict.
    kind, _ = name_conflict("Hawk Brawl", RIFTER, [{"type_id": AUTOCANNON, "quantity": 1}])
    assert kind == "conflict"
    # Free name → None.
    kind, _ = name_conflict("Totally New", RIFTER, [{"type_id": AUTOCANNON, "quantity": 2}])
    assert kind is None


@pytest.mark.django_db
def test_import_fits_is_director_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", 7104, rbac.ROLE_MEMBER))
    assert client.get(reverse("admin_audit:import_fits")).status_code == 403


@pytest.mark.django_db
def test_leader_can_delete_a_doctrine_from_the_list(client, django_user_model, sde):
    from apps.doctrines.models import Doctrine as D
    client.force_login(_user(django_user_model, "off", 7112, rbac.ROLE_OFFICER))
    d = D.objects.create(name="Throwaway")
    resp = client.post(reverse("admin_audit:doctrine_delete", args=[d.pk]))
    assert resp.status_code == 302
    assert not D.objects.filter(pk=d.pk).exists()


# --- killmail → doctrine -----------------------------------------------------
@pytest.mark.django_db
def test_doctrine_from_killmail_get_prefills_eft(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "dir", 7105, rbac.ROLE_DIRECTOR))
    km = _loss_with_fit(920002)
    html = client.get(reverse("admin_audit:doctrine_from_killmail",
                              args=[km.killmail_id])).content.decode()
    assert "Fusion S x100" in html  # cargo pre-filled for fine-tuning
    assert "Rifter doctrine" in html  # suggested name


@pytest.mark.django_db
def test_doctrine_from_killmail_post_creates_doctrine(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "dir", 7106, rbac.ROLE_DIRECTOR))
    km = _loss_with_fit(920003)
    eft = "[Rifter, Brawl Rifter]\n200mm AutoCannon I x2\nFusion S x200\n"
    resp = client.post(reverse("admin_audit:doctrine_from_killmail", args=[km.killmail_id]), {
        "name": "Rifter Brawl", "eft": eft, "priority": "60", "role": "Tackle",
    })
    assert resp.status_code == 302
    doctrine = Doctrine.objects.get(name="Rifter Brawl")
    fit = doctrine.fits.get()
    assert fit.ship_type_id == RIFTER and fit.role == "Tackle"
    # Ammo the director added/kept is in the doctrine.
    assert any(m["type_id"] == FUSION for m in fit.modules)


@pytest.mark.django_db
def test_doctrine_from_killmail_is_director_only(client, django_user_model, sde):
    km = _loss_with_fit(920004)
    client.force_login(_user(django_user_model, "m", 7107, rbac.ROLE_MEMBER))
    assert client.get(reverse("admin_audit:doctrine_from_killmail",
                              args=[km.killmail_id])).status_code == 403


@pytest.mark.django_db
def test_killmail_detail_shows_import_button_only_for_directors(client, django_user_model, sde):
    km = _loss_with_fit(920005)
    url = reverse("killboard:detail", args=[km.killmail_id])
    import_url = reverse("admin_audit:doctrine_from_killmail", args=[km.killmail_id])

    client.force_login(_user(django_user_model, "m", 7108, rbac.ROLE_MEMBER))
    assert import_url not in client.get(url).content.decode()

    client.force_login(_user(django_user_model, "dir", 7109, rbac.ROLE_DIRECTOR))
    assert import_url in client.get(url).content.decode()
