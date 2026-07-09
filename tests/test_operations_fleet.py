"""Fleet planner: ship slots, race-safe sign-ups, auto-cancel, overrides, perms."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.operations import services
from apps.operations.models import (
    Operation,
    OperationCancellation,
    OperationCommitment,
    OperationShipSlot,
)
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, cid, role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=user, name=f"Pilot{cid}",
                                is_main=True, is_corp_member=True)
    return user


def _op(**kw):
    kw.setdefault("name", "Saturday Fleet")
    kw.setdefault("type", Operation.Type.PVP)
    kw.setdefault("target_at", timezone.now() + timedelta(days=1))
    return Operation.objects.create(**kw)


def _slot(op, ship, *, min_pilots=1, max_pilots=None, priority=1, role="dps"):
    return OperationShipSlot.objects.create(
        operation=op, ship_name=ship, min_pilots=min_pilots, max_pilots=max_pilots,
        priority=priority, role=role,
    )


# --- creation & validation ---------------------------------------------------
@pytest.mark.django_db
def test_officer_creates_op_with_slots_deadline_and_srp(client, django_user_model, sde):
    officer = _user(django_user_model, 7001, rbac.ROLE_OFFICER)
    client.force_login(officer)
    start = timezone.now() + timedelta(days=2)
    resp = client.post("/operations/create/", {
        "name": "CTA", "type": "pvp", "status": "planned",
        "target_at": start.strftime("%Y-%m-%dT%H:%M"),
        "min_pilots": "2", "rsvp_mode": "relative", "rsvp_offset_minutes": "60",
        "srp": "alliance",
        "slot_ship": ["Guardian", "Megathron"], "slot_role": ["logi", "dps"],
        "slot_min": ["1", "1"], "slot_max": ["", ""], "slot_priority": ["1", "2"],
        "slot_link": ["", ""],
    })
    assert resp.status_code == 302
    op = Operation.objects.get(name="CTA")
    assert op.ship_slots.count() == 2
    assert op.srp == "alliance" and op.min_pilots == 2
    # Relative deadline resolved to 60 min before form-up.
    assert op.rsvp_deadline is not None
    assert abs((op.target_at - op.rsvp_deadline).total_seconds() - 3600) < 5


@pytest.mark.django_db
def test_deadline_after_start_is_rejected(client, django_user_model, sde):
    client.force_login(_user(django_user_model, 7002, rbac.ROLE_OFFICER))
    start = timezone.now() + timedelta(days=1)
    after = start + timedelta(hours=2)
    resp = client.post("/operations/create/", {
        "name": "Bad", "type": "pvp", "status": "planned",
        "target_at": start.strftime("%Y-%m-%dT%H:%M"),
        "rsvp_mode": "absolute", "rsvp_deadline": after.strftime("%Y-%m-%dT%H:%M"),
        "slot_ship": [""],
    })
    assert resp.status_code == 200  # re-rendered form with the error
    assert not Operation.objects.filter(name="Bad").exists()


@pytest.mark.django_db
def test_slot_mismatch_needs_confirmation(client, django_user_model, sde):
    client.force_login(_user(django_user_model, 7003, rbac.ROLE_OFFICER))
    base = {
        "name": "Mismatch", "type": "pvp", "status": "planned",
        "min_pilots": "5",  # but slots only sum to 1
        "slot_ship": ["Rifter"], "slot_role": ["tackle"], "slot_min": ["1"],
        "slot_max": [""], "slot_priority": ["1"], "slot_link": [""],
    }
    assert client.post("/operations/create/", base).status_code == 200
    assert not Operation.objects.filter(name="Mismatch").exists()
    # With the confirmation ticked it goes through.
    resp = client.post("/operations/create/", {**base, "confirm_mismatch": "1"})
    assert resp.status_code == 302
    assert Operation.objects.filter(name="Mismatch").exists()


def _doctrine_fit(name="Mega Fleet", ship_type_id=641, status=None, role="DPS"):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", defaults={"label": "DPS"})
    d = Doctrine.objects.create(name=name, category=cat, status=status or Doctrine.Status.ACTIVE)
    return DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=ship_type_id, role=role)


@pytest.mark.django_db
def test_create_with_doctrine_slot(client, django_user_model, sde):
    fit = _doctrine_fit(ship_type_id=641)
    client.force_login(_user(django_user_model, 7011, rbac.ROLE_OFFICER))
    resp = client.post("/operations/create/", {
        "name": "Doctrine Op", "type": "pvp", "status": "planned", "min_pilots": "2",
        "slot_kind": ["doctrine"], "slot_fit_id": [str(fit.id)], "slot_ship": [""],
        "slot_eft": [""], "slot_role": ["dps"], "slot_min": ["2"], "slot_max": [""],
        "slot_priority": ["1"],
    })
    assert resp.status_code == 302
    slot = Operation.objects.get(name="Doctrine Op").ship_slots.get()
    assert slot.doctrine_fit_id == fit.id and slot.is_doctrine
    assert slot.ship_type_id == 641 and slot.min_pilots == 2


@pytest.mark.django_db
def test_create_with_custom_eft_slot(client, django_user_model, sde):
    client.force_login(_user(django_user_model, 7012, rbac.ROLE_OFFICER))
    eft = "[Rifter, Solo]\nDamage Control II\n200mm AutoCannon II"
    resp = client.post("/operations/create/", {
        "name": "Custom Op", "type": "roam", "status": "planned", "min_pilots": "1",
        "slot_kind": ["custom"], "slot_fit_id": [""], "slot_ship": ["Rifter"],
        "slot_eft": [eft], "slot_role": ["tackle"], "slot_min": ["1"], "slot_max": [""],
        "slot_priority": ["1"],
    })
    assert resp.status_code == 302
    slot = Operation.objects.get(name="Custom Op").ship_slots.get()
    assert not slot.is_doctrine and slot.doctrine_fit_id is None
    assert "Damage Control II" in slot.eft_text and slot.role == "tackle"


@pytest.mark.django_db
def test_inactive_doctrine_fit_is_rejected(client, django_user_model, sde):
    from apps.doctrines.models import Doctrine

    fit = _doctrine_fit(name="Retired", ship_type_id=641, status=Doctrine.Status.RETIRED)
    client.force_login(_user(django_user_model, 7013, rbac.ROLE_OFFICER))
    resp = client.post("/operations/create/", {
        "name": "Bad Doctrine", "type": "pvp", "status": "planned",
        "slot_kind": ["doctrine"], "slot_fit_id": [str(fit.id)], "slot_ship": [""],
        "slot_eft": [""], "slot_role": ["dps"], "slot_min": ["1"], "slot_max": [""],
        "slot_priority": ["1"],
    })
    assert resp.status_code == 302
    assert Operation.objects.get(name="Bad Doctrine").ship_slots.count() == 0  # dropped


@pytest.mark.django_db
def test_doctrine_fit_catalogue_lists_active_only_with_category():
    from apps.doctrines.models import Doctrine

    active = _doctrine_fit(name="Active Doc", ship_type_id=641)
    _doctrine_fit(name="Old Doc", ship_type_id=642, status=Doctrine.Status.RETIRED)
    cat = services.doctrine_fit_catalogue()
    fit_ids = {f["fit_id"] for f in cat["fits"]}
    assert active.id in fit_ids
    assert all(f["doctrine"] != "Old Doc" for f in cat["fits"])  # retired excluded
    row = next(f for f in cat["fits"] if f["fit_id"] == active.id)
    assert row["category"] == "DPS" and "categories" in cat


@pytest.mark.django_db
def test_ship_search_returns_ships_only(client, django_user_model):
    from apps.sde.models import SdeCategory, SdeGroup, SdeType

    ships = SdeCategory.objects.create(category_id=6, name="Ship")
    other = SdeCategory.objects.create(category_id=7, name="Module")
    frig = SdeGroup.objects.create(group_id=25, category=ships, name="Frigate")
    mod = SdeGroup.objects.create(group_id=60, category=other, name="Afterburner")
    SdeType.objects.create(type_id=587, group=frig, name="Rifter", published=True)
    SdeType.objects.create(type_id=438, group=mod, name="Rifling Module", published=True)

    client.force_login(_user(django_user_model, 7014, rbac.ROLE_OFFICER))
    rows = client.get("/operations/ship-search/?q=Rif").json()
    names = {r["name"] for r in rows}
    assert "Rifter" in names and "Rifling Module" not in names  # ships only


@pytest.mark.django_db
def test_ship_search_is_officer_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, 7015, rbac.ROLE_MEMBER))
    assert client.get("/operations/ship-search/?q=rifter").status_code == 403


# --- claiming slots ----------------------------------------------------------
@pytest.mark.django_db
def test_commit_reduces_still_needed(django_user_model, sde):
    op = _op(min_pilots=2)
    slot = _slot(op, "Guardian", min_pilots=2)
    u = _user(django_user_model, 7101)
    assert services.claim_slot(op, u, slot.id, u.characters.first()) == services.CLAIM_OK
    plan = services.fleet_plan(op)
    row = plan["slots"][0]
    assert row["confirmed"] == 1 and row["still_needed"] == 1 and not row["met"]


@pytest.mark.django_db
def test_overbooking_blocked_by_max(django_user_model, sde):
    op = _op(min_pilots=1)
    slot = _slot(op, "Falcon", min_pilots=1, max_pilots=1)
    u1 = _user(django_user_model, 7201)
    u2 = _user(django_user_model, 7202)
    assert services.claim_slot(op, u1, slot.id, u1.characters.first()) == services.CLAIM_OK
    # The single seat is taken — the second pilot is turned away, not overbooked.
    assert services.claim_slot(op, u2, slot.id, u2.characters.first()) == services.CLAIM_FULL
    assert OperationCommitment.objects.filter(operation=op, slot=slot).count() == 1


@pytest.mark.django_db
def test_extra_pilots_join_uncapped_slot_after_min_met(django_user_model, sde):
    op = _op(min_pilots=1)
    slot = _slot(op, "Megathron", min_pilots=1)  # no max → unlimited
    for cid in (7301, 7302, 7303):
        u = _user(django_user_model, cid)
        assert services.claim_slot(op, u, slot.id, u.characters.first()) == services.CLAIM_OK
    plan = services.fleet_plan(op)
    assert plan["min_met"] and plan["total_confirmed"] == 3
    assert plan["slots"][0]["confirmed"] == 3  # extras welcome beyond the minimum


@pytest.mark.django_db
def test_recommended_slot_follows_priority(django_user_model, sde):
    op = _op(min_pilots=2)
    logi = _slot(op, "Guardian", min_pilots=1, priority=1)
    _slot(op, "Megathron", min_pilots=1, priority=2)
    plan = services.fleet_plan(op)
    assert plan["recommended_slot_id"] == logi.id  # highest priority, still short
    u = _user(django_user_model, 7401)
    services.claim_slot(op, u, logi.id, u.characters.first())
    plan = services.fleet_plan(op)
    assert plan["recommended_slot_id"] != logi.id  # now points at the next gap


@pytest.mark.django_db
def test_switch_ship_keeps_single_commitment(django_user_model, sde):
    op = _op(min_pilots=1)
    a = _slot(op, "Guardian", priority=1)
    b = _slot(op, "Megathron", priority=2)
    u = _user(django_user_model, 7501)
    services.claim_slot(op, u, a.id, u.characters.first())
    services.claim_slot(op, u, b.id, u.characters.first())
    commitments = OperationCommitment.objects.filter(operation=op, user=u)
    assert commitments.count() == 1 and commitments.first().slot_id == b.id


@pytest.mark.django_db
def test_uncommit_releases_slot(django_user_model, sde):
    op = _op(min_pilots=1)
    slot = _slot(op, "Guardian")
    u = _user(django_user_model, 7601)
    services.claim_slot(op, u, slot.id, u.characters.first())
    assert services.release_commitment(op, u) is True
    assert services.fleet_plan(op)["total_confirmed"] == 0


@pytest.mark.django_db
def test_commit_closed_after_deadline(django_user_model, sde):
    op = _op(min_pilots=1, target_at=timezone.now() + timedelta(hours=1),
             rsvp_deadline=timezone.now() - timedelta(minutes=5))
    slot = _slot(op, "Guardian")
    u = _user(django_user_model, 7701)
    assert services.claim_slot(op, u, slot.id, u.characters.first()) == services.CLAIM_CLOSED
    assert OperationCommitment.objects.filter(operation=op).count() == 0


# --- coming vs maybe (ship-tied confirmation) --------------------------------
@pytest.mark.django_db
def test_maybe_does_not_count_toward_minimum(django_user_model, sde):
    op = _op(min_pilots=1)
    slot = _slot(op, "Megathron", min_pilots=1)
    u = _user(django_user_model, 8801)
    assert services.claim_slot(op, u, slot.id, u.characters.first(), response="maybe") == services.CLAIM_OK
    plan = services.fleet_plan(op)
    assert plan["total_confirmed"] == 0 and plan["total_maybe"] == 1
    assert not plan["min_met"]
    row = plan["slots"][0]
    assert row["confirmed"] == 0 and row["maybe"] == 1 and row["still_needed"] == 1


@pytest.mark.django_db
def test_switch_between_coming_and_maybe(django_user_model, sde):
    from apps.operations.models import OperationCommitment

    op = _op(min_pilots=1)
    slot = _slot(op, "Megathron", min_pilots=1)
    u = _user(django_user_model, 8802)
    char = u.characters.first()
    services.claim_slot(op, u, slot.id, char, response="yes")
    assert services.fleet_plan(op)["total_confirmed"] == 1
    services.claim_slot(op, u, slot.id, char, response="maybe")  # same row, downgraded
    assert OperationCommitment.objects.filter(operation=op, user=u).count() == 1
    plan = services.fleet_plan(op)
    assert plan["total_confirmed"] == 0 and plan["total_maybe"] == 1


@pytest.mark.django_db
def test_maybe_never_overbooks_capped_slot(django_user_model, sde):
    op = _op(min_pilots=1)
    slot = _slot(op, "Falcon", min_pilots=1, max_pilots=1)
    u1 = _user(django_user_model, 8803)
    u2 = _user(django_user_model, 8804)
    assert services.claim_slot(op, u1, slot.id, u1.characters.first(), response="yes") == services.CLAIM_OK
    # A "maybe" is a soft signal — it doesn't take the seat, so it's allowed.
    assert services.claim_slot(op, u2, slot.id, u2.characters.first(), response="maybe") == services.CLAIM_OK
    # But a second firm "coming" is still blocked by the cap.
    u3 = _user(django_user_model, 8805)
    assert services.claim_slot(op, u3, slot.id, u3.characters.first(), response="yes") == services.CLAIM_FULL


@pytest.mark.django_db
def test_commit_view_records_chosen_ship_and_response(client, django_user_model, sde):
    from apps.operations.models import OperationCommitment

    member = _user(django_user_model, 8806, rbac.ROLE_MEMBER)
    op = _op(min_pilots=1)
    slot = _slot(op, "Guardian")
    client.force_login(member)
    # Saying "maybe" still requires (and records) the ship choice.
    assert client.post(f"/operations/{op.pk}/commit/",
                       {"slot_id": slot.id, "response": "maybe"}).status_code == 302
    c = OperationCommitment.objects.get(operation=op, user=member)
    assert c.slot_id == slot.id and c.response == "maybe"


@pytest.mark.django_db
def test_cant_make_it_removes_commitment(client, django_user_model, sde):
    from apps.operations.models import OperationCommitment, OperationRsvp

    member = _user(django_user_model, 8807, rbac.ROLE_MEMBER)
    op = _op(min_pilots=1)
    slot = _slot(op, "Guardian")
    client.force_login(member)
    client.post(f"/operations/{op.pk}/commit/", {"slot_id": slot.id, "response": "yes"})
    assert OperationCommitment.objects.filter(operation=op, user=member).exists()
    # Marking "can't make it" drops the commitment and records the no.
    client.post(f"/operations/{op.pk}/rsvp/", {"response": "no"})
    assert not OperationCommitment.objects.filter(operation=op, user=member).exists()
    assert OperationRsvp.objects.get(operation=op, user=member).response == "no"


# --- auto-cancellation -------------------------------------------------------
@pytest.mark.django_db
def test_auto_cancel_when_below_minimum(django_user_model, sde):
    op = _op(min_pilots=3, target_at=timezone.now() + timedelta(hours=2),
             rsvp_deadline=timezone.now() - timedelta(minutes=1))
    slot = _slot(op, "Megathron", min_pilots=3)
    u = _user(django_user_model, 7801)
    OperationCommitment.objects.create(operation=op, user=u, slot=slot, character_name="P")

    cancelled = services.auto_cancel_due()
    assert op.pk in cancelled
    op.refresh_from_db()
    assert op.status == Operation.Status.CANCELLED_AUTO
    rec = OperationCancellation.objects.get(operation=op)
    assert rec.reason == OperationCancellation.Reason.INSUFFICIENT
    assert rec.min_pilots == 3 and rec.confirmed_at_deadline == 1
    assert rec.required_composition == {"Megathron": 3}
    assert rec.actual_composition == {"Megathron": 1}


@pytest.mark.django_db
def test_auto_cancel_when_composition_unmet(django_user_model, sde):
    # Head-count is met (2 ≥ 2) but both stacked on one slot, leaving a gap.
    op = _op(min_pilots=2, rsvp_deadline=timezone.now() - timedelta(minutes=1))
    a = _slot(op, "Guardian", min_pilots=1, priority=1)
    _slot(op, "Megathron", min_pilots=1, priority=2)
    for cid in (7901, 7902):
        u = _user(django_user_model, cid)
        OperationCommitment.objects.create(operation=op, user=u, slot=a, character_name=f"P{cid}")

    services.auto_cancel_due()
    op.refresh_from_db()
    assert op.status == Operation.Status.CANCELLED_AUTO
    assert OperationCancellation.objects.get(operation=op).reason == \
        OperationCancellation.Reason.COMPOSITION


@pytest.mark.django_db
def test_auto_cancel_skips_when_requirements_met(django_user_model, sde):
    op = _op(min_pilots=1, rsvp_deadline=timezone.now() - timedelta(minutes=1))
    slot = _slot(op, "Megathron", min_pilots=1)
    u = _user(django_user_model, 8001)
    OperationCommitment.objects.create(operation=op, user=u, slot=slot, character_name="P")
    assert services.auto_cancel_due() == []
    op.refresh_from_db()
    assert op.status == Operation.Status.PLANNED


@pytest.mark.django_db
def test_override_prevents_auto_cancel(django_user_model, sde):
    op = _op(min_pilots=10, rsvp_deadline=timezone.now() - timedelta(minutes=1),
             requirements_overridden=True)
    _slot(op, "Megathron", min_pilots=10)
    assert services.auto_cancel_due() == []
    op.refresh_from_db()
    assert op.status == Operation.Status.PLANNED
    assert services.fleet_plan(op)["viable"] is True  # override makes it viable


# --- manual cancel records, posture ------------------------------------------
@pytest.mark.django_db
def test_manual_cancel_records_snapshot(client, django_user_model, sde):
    officer = _user(django_user_model, 8101, rbac.ROLE_OFFICER)
    op = _op(min_pilots=5)
    _slot(op, "Megathron", min_pilots=5)
    client.force_login(officer)
    resp = client.post(f"/operations/{op.pk}/status/", {"status": "cancelled"})
    assert resp.status_code == 302
    op.refresh_from_db()
    assert op.status == Operation.Status.CANCELLED
    rec = OperationCancellation.objects.get(operation=op)
    assert rec.reason == OperationCancellation.Reason.MANUAL


@pytest.mark.django_db
def test_posture_reflects_state(django_user_model, sde):
    op = _op(min_pilots=2)
    slot = _slot(op, "Megathron", min_pilots=2)
    assert services.fleet_plan(op)["posture"] == "scheduled"
    for cid in (8201, 8202):
        u = _user(django_user_model, cid)
        OperationCommitment.objects.create(operation=op, user=u, slot=slot, character_name="P")
    assert services.fleet_plan(op)["posture"] == "ready"


# --- permissions -------------------------------------------------------------
@pytest.mark.django_db
def test_unsafe_link_scheme_is_dropped(client, django_user_model, sde):
    client.force_login(_user(django_user_model, 8701, rbac.ROLE_OFFICER))
    resp = client.post("/operations/create/", {
        "name": "XSS", "type": "pvp", "status": "planned",
        "link": "javascript:alert(1)",
        "slot_ship": ["Rifter"], "slot_role": ["tackle"], "slot_min": ["1"],
        "slot_max": [""], "slot_priority": ["1"], "slot_link": ["javascript:evil()"],
    })
    assert resp.status_code == 302
    op = Operation.objects.get(name="XSS")
    assert op.link == ""  # javascript: scheme stripped
    assert op.ship_slots.first().fitting_link == ""


@pytest.mark.django_db
def test_member_cannot_create_or_edit_or_override(client, django_user_model, sde):
    member = _user(django_user_model, 8301, rbac.ROLE_MEMBER)
    op = _op(min_pilots=1)
    client.force_login(member)
    assert client.get("/operations/create/").status_code == 403
    assert client.get(f"/operations/{op.pk}/edit/").status_code == 403
    assert client.post(f"/operations/{op.pk}/override/", {"override": "1"}).status_code == 403


# --- cancellation analytics (OPS-8) ------------------------------------------
@pytest.mark.django_db
def test_cancellation_analytics_officer_only(client, django_user_model, sde):
    officer = _user(django_user_model, 8801, rbac.ROLE_OFFICER)
    op = _op(min_pilots=5)
    _slot(op, "Megathron", min_pilots=5)
    client.force_login(officer)
    # Seed a snapshot via the manual-cancel flow, then read the analytics page.
    client.post(f"/operations/{op.pk}/status/", {"status": "cancelled"})
    assert OperationCancellation.objects.count() == 1
    resp = client.get("/operations/analytics/cancellations/")
    assert resp.status_code == 200
    assert b"Cancellation analytics" in resp.content
    assert b"ops-cancel-data" in resp.content  # chart payload rendered

    member = _user(django_user_model, 8802, rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.get("/operations/analytics/cancellations/").status_code == 403


@pytest.mark.django_db
def test_create_and_edit_forms_render(client, django_user_model, sde):
    officer = _user(django_user_model, 8501, rbac.ROLE_OFFICER)
    op = _op(min_pilots=2, formup="Jita 4-4", rsvp_offset_minutes=60,
             rsvp_deadline=timezone.now() + timedelta(hours=20))
    _slot(op, "Guardian", min_pilots=2, max_pilots=3, role="logi")
    client.force_login(officer)
    create = client.get("/operations/create/")
    assert create.status_code == 200 and b"Fleet composition" in create.content
    edit = client.get(f"/operations/{op.pk}/edit/")  # prefilled datetimes must format
    assert edit.status_code == 200 and b"Guardian" in edit.content


@pytest.mark.django_db
def test_detail_shows_composition_and_srp(client, django_user_model, sde):
    member = _user(django_user_model, 8601, rbac.ROLE_MEMBER)
    op = _op(min_pilots=1, type=Operation.Type.PVP, srp=Operation.Srp.ALLIANCE)
    _slot(op, "Guardian", min_pilots=1, role="logi")
    client.force_login(member)
    html = client.get(f"/operations/{op.pk}/").content
    assert b"Fleet composition" in html and b"Guardian" in html
    assert b"Alliance SRP" in html  # SRP visible to pilots before they commit


@pytest.mark.django_db
def test_member_can_commit_and_withdraw_via_views(client, django_user_model, sde):
    member = _user(django_user_model, 8401, rbac.ROLE_MEMBER)
    op = _op(min_pilots=1)
    slot = _slot(op, "Guardian")
    client.force_login(member)
    assert client.post(f"/operations/{op.pk}/commit/", {"slot_id": slot.id}).status_code == 302
    assert OperationCommitment.objects.filter(operation=op, user=member).count() == 1
    assert client.post(f"/operations/{op.pk}/uncommit/", {}).status_code == 302
    assert OperationCommitment.objects.filter(operation=op, user=member).count() == 0


# --- edit recomputes deadline (start-time change) ----------------------------
@pytest.mark.django_db
def test_recompute_deadline_tracks_start_change(django_user_model, sde):
    op = _op(target_at=timezone.now() + timedelta(days=1), rsvp_offset_minutes=120)
    services.recompute_deadline(op)
    first = op.rsvp_deadline
    assert abs((op.target_at - first).total_seconds() - 7200) < 5
    # Move the form-up later → deadline tracks it.
    op.target_at = op.target_at + timedelta(days=1)
    services.recompute_deadline(op)
    assert op.rsvp_deadline > first
    assert abs((op.target_at - op.rsvp_deadline).total_seconds() - 7200) < 5


# --- readiness doctrines are derived from the chosen doctrine ships -----------
@pytest.mark.django_db
def test_doctrine_slots_derive_readiness_doctrines(client, django_user_model, sde):
    """Picking doctrine ships in the composition makes those doctrines the op's
    readiness doctrines automatically — no separate doctrine-pick step. The pilots
    wanted on a doctrine is the sum of its slots' minimums; custom ships add none."""
    fit = _doctrine_fit(name="Mega Fleet", ship_type_id=641)
    client.force_login(_user(django_user_model, 7050, rbac.ROLE_OFFICER))
    resp = client.post("/operations/create/", {
        "name": "Derived Op", "type": "pvp", "status": "planned", "min_pilots": "4",
        # Two slots on the SAME doctrine (min 2 + 1) plus one custom hull.
        "slot_kind": ["doctrine", "doctrine", "custom"],
        "slot_fit_id": [str(fit.id), str(fit.id), ""],
        "slot_ship": ["", "", "Rifter"], "slot_eft": ["", "", ""],
        "slot_role": ["dps", "dps", "tackle"],
        "slot_min": ["2", "1", "1"], "slot_max": ["", "", ""],
        "slot_priority": ["1", "2", "3"],
    })
    assert resp.status_code == 302
    op = Operation.objects.get(name="Derived Op")
    # Exactly one OperationDoctrine row, target = 2 + 1 (custom hull contributes none).
    od = op.doctrines.get()
    assert od.doctrine_id == fit.doctrine_id
    assert od.target_count == 3


@pytest.mark.django_db
def test_editing_slots_resyncs_derived_doctrines(client, django_user_model, sde):
    """Removing the doctrine ship on edit drops the derived readiness doctrine."""
    fit = _doctrine_fit(name="Logi Wing", ship_type_id=11985)
    client.force_login(_user(django_user_model, 7051, rbac.ROLE_OFFICER))
    op = _op(name="Resync Op", created_by=None)
    OperationShipSlot.objects.create(
        operation=op, ship_name="Guardian", ship_type_id=11985,
        doctrine_fit=fit, min_pilots=1, priority=1, role="logi",
    )
    from apps.operations.models import OperationDoctrine
    OperationDoctrine.objects.create(operation=op, doctrine=fit.doctrine, target_count=1)
    # Edit to an all-custom composition → no doctrine ships left.
    resp = client.post(f"/operations/{op.pk}/edit/", {
        "name": "Resync Op", "type": "pvp", "status": "planned", "min_pilots": "1",
        "slot_kind": ["custom"], "slot_fit_id": [""], "slot_ship": ["Rifter"],
        "slot_eft": [""], "slot_role": ["tackle"], "slot_min": ["1"], "slot_max": [""],
        "slot_priority": ["1"],
    })
    assert resp.status_code == 302
    assert op.doctrines.count() == 0


# --- FC resolves to a pilot name + killboard link ----------------------------
@pytest.mark.django_db
def test_detail_shows_fc_as_pilot_link_not_username(client, django_user_model, sde):
    fc = _user(django_user_model, 329791008, rbac.ROLE_OFFICER)
    op = _op(name="FC Op", fc=fc)
    client.force_login(fc)
    html = client.get(f"/operations/{op.pk}/").content.decode()
    # Renders the pilot's name + a link to their individual killboard, never the
    # opaque eve:<id> account username.
    assert f'/killboard/pilot/{329791008}/' in html
    assert "Pilot329791008" in html
    assert "eve:329791008" not in html
