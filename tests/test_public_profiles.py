"""Public pilot profiles (apps.profiles) — the unlisted, opt-in share page.

Covers the owner half (publish / update / rotate / toggle / delete, the IDOR shape, membership
and feature gating, audit rows), the visitor half (token format, the ONE constant-shape 404 for
every failure mode, headers, noindex, sections, the password gate + its throttle, owner bypass,
view counting), the "never leaks the account's other pilots" guarantee, and the lifecycle hooks
(unlink and data erasure take a publication down).
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.characters.models import CharacterAttributes, CharacterSkillSnapshot, SkillQueueSnapshot
from apps.identity.models import RoleAssignment
from apps.profiles import services
from apps.profiles.models import TOKEN_RE, PublicProfile, new_token
from apps.profiles.sections import DEFAULT_SECTIONS, SECTIONS, clean_sections
from apps.profiles.services import ProfileError, PublishInput, expiry_from_form
from apps.sde.models import SdeCategory, SdeGroup, SdeType, SdeTypeSkill
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from core.features import set_disabled

pytestmark = pytest.mark.django_db

User = get_user_model()

MANAGE = "/public-profile/"


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _member(username="eve:2001", *, main_id=2001, name="Sharer One") -> tuple:
    user = User.objects.create(username=username, first_name=name)
    user.set_unusable_password()
    user.save()
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    char = EveCharacter.objects.create(
        character_id=main_id, user=user, name=name, is_main=True, is_corp_member=True
    )
    return user, char


def _skill_types():
    """A tiny SDE: two skill types in a "Spaceship Command" group + one frigate hull."""
    cat_skill, _ = SdeCategory.objects.get_or_create(category_id=16, defaults={"name": "Skill"})
    cat_ship, _ = SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    g_skill, _ = SdeGroup.objects.get_or_create(
        group_id=257, defaults={"category": cat_skill, "name": "Spaceship Command"}
    )
    g_frig, _ = SdeGroup.objects.get_or_create(
        group_id=25, defaults={"category": cat_ship, "name": "Frigate"}
    )
    SdeType.objects.get_or_create(type_id=3327, defaults={"group": g_skill, "name": "Spaceship Command", "rank": 1})
    SdeType.objects.get_or_create(type_id=3330, defaults={"group": g_skill, "name": "Caldari Frigate", "rank": 2})
    SdeType.objects.get_or_create(type_id=3331, defaults={"group": g_skill, "name": "Amarr Frigate", "rank": 2})
    kestrel, _ = SdeType.objects.get_or_create(
        type_id=602, defaults={"group": g_frig, "name": "Kestrel", "published": True}
    )
    SdeTypeSkill.objects.get_or_create(type=kestrel, skill_type_id=3330, defaults={"level": 1})
    punisher, _ = SdeType.objects.get_or_create(
        type_id=597, defaults={"group": g_frig, "name": "Punisher", "published": True}
    )
    SdeTypeSkill.objects.get_or_create(type=punisher, skill_type_id=3331, defaults={"level": 1})


def _snapshot(char, skills=None):
    skills = skills or {"3327": {"trained_level": 5, "sp": 256000},
                        "3330": {"trained_level": 4, "sp": 90510}}
    return CharacterSkillSnapshot.objects.create(
        character=char, skills=skills, total_sp=sum(v["sp"] for v in skills.values()), is_latest=True
    )


def _publish(user, char, **kw) -> PublicProfile:
    data = PublishInput(
        sections=kw.pop("sections", list(DEFAULT_SECTIONS)),
        expires_at=kw.pop("expires_at", None),
        note=kw.pop("note", ""),
        password=kw.pop("password", None),
    )
    profile, _created = services.publish(user, char, data)
    return profile


def _url(profile) -> str:
    return f"/p/{profile.token}/"


# --------------------------------------------------------------------------- #
#  Model + registry
# --------------------------------------------------------------------------- #
def test_tokens_are_long_urlsafe_and_unique():
    tokens = {new_token() for _ in range(50)}
    assert len(tokens) == 50
    for t in tokens:
        assert TOKEN_RE.match(t) and len(t) == 32


def test_expiry_and_password_helpers():
    user, char = _member()
    p = _publish(user, char)
    assert p.is_live and not p.is_expired and not p.has_password
    p.expires_at = timezone.now() - dt.timedelta(seconds=1)
    assert p.is_expired and not p.is_live
    p.set_password("hunter2")
    assert p.has_password and p.check_password("hunter2") and not p.check_password("nope")
    fp = p.password_fingerprint()
    p.set_password("other")
    assert p.password_fingerprint() != fp  # a changed password invalidates old grants
    p.set_password(None)
    assert not p.has_password and not p.check_password("")


def test_sections_registry_is_privacy_first():
    keys = [s.key for s in SECTIONS]
    assert keys == ["skills", "queue", "ships", "attributes", "combat"]
    assert DEFAULT_SECTIONS == ["skills"]  # only what was asked for is on by default
    assert clean_sections(["combat", "junk", "skills", "skills"]) == ["skills", "combat"]
    # The corp's own context never becomes shareable.
    for forbidden in ("doctrines", "plans", "readiness", "contributions", "assets", "alts"):
        assert forbidden not in keys


def test_expiry_from_form():
    assert expiry_from_form("never", "") is None
    soon = expiry_from_form("7d", "")
    assert dt.timedelta(days=6, hours=23) < soon - timezone.now() <= dt.timedelta(days=7)
    day = (timezone.now() + dt.timedelta(days=3)).date()
    custom = expiry_from_form("custom", day.isoformat())
    assert custom.date() == day and custom.hour == 23 and custom.tzinfo is not None
    with pytest.raises(ProfileError):
        expiry_from_form("custom", "yesterday")
    with pytest.raises(ProfileError):
        expiry_from_form("custom", (timezone.now() - dt.timedelta(days=1)).date().isoformat())
    with pytest.raises(ProfileError):
        expiry_from_form("custom", (timezone.now() + dt.timedelta(days=4000)).date().isoformat())
    with pytest.raises(ProfileError):
        expiry_from_form("bogus", "")


# --------------------------------------------------------------------------- #
#  Visitor half — the public page
# --------------------------------------------------------------------------- #
def test_anonymous_visitor_sees_shared_skills_only(client):
    _skill_types()
    user, char = _member()
    _snapshot(char)
    # A second pilot on the same account must never be mentioned on the page.
    EveCharacter.objects.create(character_id=2002, user=user, name="Secret Alt", is_corp_member=True)
    p = _publish(user, char, note="Applying for your incursion community")
    resp = client.get(_url(p))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Sharer One" in body
    assert "Spaceship Command" in body and "Caldari Frigate" in body
    assert "Applying for your incursion community" in body
    assert "Secret Alt" not in body
    # Only the chosen section renders — no queue, ships, attributes or combat blocks.
    assert "Training queue" not in body
    assert "Ships this pilot can fly" not in body
    assert "Combat record" not in body
    # Never indexed, never cached, never leaks the token as a Referer.
    assert resp["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert resp["Cache-Control"] == "private, no-store"
    assert resp["Referrer-Policy"] == "no-referrer"
    assert '<meta name="robots" content="noindex, nofollow">' in body
    # The edge appends its own Referrer-Policy header after Django's; the in-document meta wins.
    assert '<meta name="referrer" content="no-referrer">' in body
    p.refresh_from_db()
    assert p.view_count == 1 and p.last_viewed_at is not None


def test_every_failure_mode_is_the_same_404(client):
    user, char = _member()
    _snapshot(char)
    live = _publish(user, char)
    baseline = client.get("/p/" + "A" * 32 + "/")  # well-formed, unknown
    assert baseline.status_code == 404
    unknown_body = baseline.content

    def same_404(url):
        r = client.get(url)
        assert r.status_code == 404
        assert r.content == unknown_body
        assert r["X-Robots-Tag"] == "noindex, nofollow, noarchive"
        assert r["Cache-Control"] == "private, no-store"

    # Malformed tokens never reach the resolver (and never a different shape).
    same_404("/p/short/")
    same_404("/p/" + "%2e" * 16 + "/")
    # Disabled.
    live.enabled = False
    live.save()
    same_404(_url(live))
    live.enabled = True
    # Expired.
    live.expires_at = timezone.now() - dt.timedelta(minutes=1)
    live.save()
    same_404(_url(live))
    live.expires_at = None
    live.save()
    assert client.get(_url(live)).status_code == 200
    # Character re-linked to another account since publication.
    other = User.objects.create(username="eve:9")
    char.user = other
    char.save(update_fields=["user"])
    same_404(_url(live))
    char.user = user
    char.save(update_fields=["user"])
    # Publishing account no longer a member.
    RoleAssignment.objects.filter(user=user).delete()
    same_404(_url(live))


def test_feature_off_darkens_links_and_management(client):
    user, char = _member()
    p = _publish(user, char)
    set_disabled(["public_profiles"])
    assert client.get(_url(p)).status_code == 404
    client.force_login(user)
    assert client.get(MANAGE).status_code == 404
    set_disabled([])
    assert client.get(_url(p)).status_code == 200


def test_password_gate_grants_session_and_throttles(client, settings):
    _skill_types()
    user, char = _member()
    _snapshot(char)
    p = _publish(user, char, password="open sesame")
    url = _url(p)
    locked = client.get(url)
    assert locked.status_code == 200
    body = locked.content.decode()
    assert "password protected" in body
    assert "Sharer One" not in body  # the lock screen names nobody
    assert locked["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert '<meta name="referrer" content="no-referrer">' in body
    p.refresh_from_db()
    assert p.view_count == 0  # a locked hit is not a view

    wrong = client.post(url, {"password": "nope"})
    assert wrong.status_code == 200 and "not right" in wrong.content.decode()
    right = client.post(url, {"password": "open sesame"})
    assert right.status_code == 302 and right["Location"] == url
    opened = client.get(url)
    assert opened.status_code == 200 and "Spaceship Command" in opened.content.decode()
    p.refresh_from_db()
    assert p.view_count == 1

    # Changing the password invalidates the grant.
    p.set_password("new one")
    p.save()
    assert "password protected" in client.get(url).content.decode()

    # Attempts are throttled per (IP, profile).
    settings.PROFILE_PASSWORD_RATE = 3
    for _ in range(3):
        client.post(url, {"password": "x"})
    limited = client.post(url, {"password": "new one"})
    assert limited.status_code == 429
    assert "Too many attempts" in limited.content.decode()


def test_owner_skips_password_and_is_not_counted(client):
    _skill_types()
    user, char = _member()
    _snapshot(char)
    p = _publish(user, char, password="secret")
    client.force_login(user)
    resp = client.get(_url(p))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "viewing your own public profile" in body and "Spaceship Command" in body
    p.refresh_from_db()
    assert p.view_count == 0
    # A different logged-in member with the link is a normal visitor: gated and counted.
    other, _oc = _member("eve:3001", main_id=3001, name="Colleague")
    client.force_login(other)
    assert "password protected" in client.get(_url(p)).content.decode()


def test_public_page_throttle(client, settings):
    user, char = _member()
    p = _publish(user, char)
    settings.PROFILE_PUBLIC_RATE = 2
    assert client.get(_url(p)).status_code == 200
    assert client.get(_url(p)).status_code == 200
    limited = client.get(_url(p))
    assert limited.status_code == 429 and limited["Retry-After"] == "60"


def test_logged_in_ex_member_can_still_open_a_link(client):
    """/p is allowlisted past the membership gate: a non-member account is not bounced to
    onboarding when following a link it was sent."""
    user, char = _member()
    p = _publish(user, char)
    recruit = User.objects.create(username="eve:7777")
    client.force_login(recruit)
    resp = client.get(_url(p))
    assert resp.status_code == 200
    # …but the management page is members-only: the gate bounces them.
    resp = client.get(MANAGE)
    assert resp.status_code == 302 and "/onboarding" in resp["Location"]


def test_all_sections_render_and_feature_off_hides_combat(client):
    _skill_types()
    user, char = _member()
    _snapshot(char)
    SkillQueueSnapshot.objects.create(
        character=char, is_latest=True,
        entries=[{"skill_id": 3330, "finished_level": 5, "queue_position": 0,
                  "start_date": (timezone.now() - dt.timedelta(hours=1)).isoformat(),
                  "finish_date": (timezone.now() + dt.timedelta(days=2)).isoformat()}],
    )
    CharacterAttributes.objects.create(character=char, intelligence=27, memory=21,
                                       perception=23, willpower=24, charisma=19, implants=[3327])
    p = _publish(user, char, sections=["skills", "queue", "ships", "attributes", "combat"])
    body = client.get(_url(p)).content.decode()
    assert "Training queue" in body and "Now training" in body
    assert "Ships this pilot can fly" in body and "Kestrel" in body and "Punisher" not in body
    assert "Attributes &amp; implants" in body and "27" in body
    assert "Combat record" in body and "No PvP record" in body
    set_disabled(["killboard"])
    body = client.get(_url(p)).content.decode()
    assert "Combat record" not in body
    assert "Ships this pilot can fly" in body


def test_head_and_stray_post_do_not_break(client):
    user, char = _member()
    p = _publish(user, char)
    assert client.head(_url(p)).status_code == 200
    resp = client.post(_url(p), {"password": "irrelevant"})
    assert resp.status_code == 302  # nothing to post once open — treated as a reload
    assert client.put(_url(p)).status_code == 405


# --------------------------------------------------------------------------- #
#  Owner half — management
# --------------------------------------------------------------------------- #
def test_management_requires_login(client):
    resp = client.get(MANAGE)
    assert resp.status_code == 302 and "/auth/eve/login/" in resp["Location"]


def test_publish_update_rotate_toggle_delete_flow(client):
    user, char = _member()
    client.force_login(user)
    page = client.get(MANAGE)
    assert page.status_code == 200
    assert "Not published" in page.content.decode()

    day = (timezone.now() + dt.timedelta(days=10)).date().isoformat()
    resp = client.post("/public-profile/save/", {
        "character_id": char.character_id, "sections": ["skills", "queue"],
        "expiry": "custom", "expires_on": day, "note": "  hello  ", "password": "letmein",
    })
    assert resp.status_code == 302 and resp["Location"].endswith(f"?c={char.character_id}")
    p = PublicProfile.objects.get(character=char)
    assert p.owner == user and p.sections == ["skills", "queue"] and p.note == "hello"
    assert p.expires_at.date().isoformat() == day and p.has_password and p.check_password("letmein")
    token = p.token
    page = client.get(MANAGE).content.decode()
    assert f"/p/{token}/" in page and "Live" in page

    # Update keeps the token and the password unless told otherwise.
    client.post("/public-profile/save/", {
        "character_id": char.character_id, "sections": ["skills"], "expiry": "never",
    })
    p.refresh_from_db()
    assert p.token == token and p.expires_at is None and p.has_password and p.sections == ["skills"]
    client.post("/public-profile/save/", {
        "character_id": char.character_id, "sections": ["skills"], "expiry": "30d", "clear_password": "1",
    })
    p.refresh_from_db()
    assert not p.has_password and p.expires_at is not None

    # Rotate: old link dead, new link live.
    client.post("/public-profile/rotate/", {"character_id": char.character_id})
    p.refresh_from_db()
    assert p.token != token
    client.logout()
    assert client.get(f"/p/{token}/").status_code == 404
    assert client.get(_url(p)).status_code == 200
    client.force_login(user)

    # Toggle off / on.
    client.post("/public-profile/toggle/", {"character_id": char.character_id})
    p.refresh_from_db()
    assert not p.enabled
    assert "Disabled" in client.get(MANAGE).content.decode()
    # Editing a disabled profile keeps it disabled — re-enabling is the explicit button.
    client.post("/public-profile/save/", {
        "character_id": char.character_id, "sections": ["skills"], "expiry": "never",
    })
    p.refresh_from_db()
    assert not p.enabled and p.expires_at is None
    client.post("/public-profile/toggle/", {"character_id": char.character_id})
    p.refresh_from_db()
    assert p.enabled

    # Delete.
    client.post("/public-profile/delete/", {"character_id": char.character_id})
    assert not PublicProfile.objects.filter(character=char).exists()

    actions = list(AuditLog.objects.filter(actor=user).values_list("action", flat=True))
    for expected in ("profile.published", "profile.updated", "profile.rotated",
                     "profile.disabled", "profile.enabled", "profile.deleted"):
        assert expected in actions
    # The audit trail never carries the token or the password.
    for row in AuditLog.objects.filter(actor=user):
        blob = str(row.metadata)
        assert token not in blob and "letmein" not in blob


def test_validation_errors_flash_and_do_not_publish(client):
    user, char = _member()
    client.force_login(user)
    resp = client.post("/public-profile/save/", {
        "character_id": char.character_id, "sections": ["skills"], "expiry": "custom",
        "expires_on": "not-a-date",
    }, follow=True)
    assert "YYYY-MM-DD" in resp.content.decode()
    assert not PublicProfile.objects.filter(character=char).exists()
    resp = client.post("/public-profile/save/", {
        "character_id": char.character_id, "sections": ["skills"], "expiry": "never", "password": "ab",
    }, follow=True)
    assert "at least 4 characters" in resp.content.decode()
    assert not PublicProfile.objects.filter(character=char).exists()


def test_no_sections_is_refused_and_form_reflects_saved_expiry(client):
    user, char = _member()
    client.force_login(user)
    resp = client.post("/public-profile/save/", {
        "character_id": char.character_id, "expiry": "never",
    }, follow=True)
    assert "Pick at least one thing" in resp.content.decode()
    assert not PublicProfile.objects.filter(character=char).exists()
    # A fresh form proposes the default preset; a saved profile re-opens on its own state.
    page = client.get(MANAGE).content.decode()
    assert '<option value="30d" selected>' in page
    _publish(user, char, expires_at=None)
    page = client.get(MANAGE).content.decode()
    assert '<option value="never" selected>' in page
    day = (timezone.now() + dt.timedelta(days=5)).replace(hour=23, minute=59, second=59)
    p = PublicProfile.objects.get(character=char)
    p.expires_at = day
    p.save()
    page = client.get(MANAGE).content.decode()
    assert '<option value="custom" selected>' in page
    assert f'value="{day.date().isoformat()}"' in page


def test_cannot_publish_or_touch_someone_elses_pilot(client):
    owner, char = _member()
    intruder, _ic = _member("eve:3001", main_id=3001, name="Intruder")
    _publish(owner, char)
    client.force_login(intruder)
    for path in ("/public-profile/save/", "/public-profile/rotate/",
                 "/public-profile/toggle/", "/public-profile/delete/"):
        resp = client.post(path, {"character_id": char.character_id, "sections": ["skills"],
                                  "expiry": "never"}, follow=True)
        assert "not linked to your account" in resp.content.decode()
    p = PublicProfile.objects.get(character=char)
    assert p.owner == owner and p.enabled
    assert PublicProfile.objects.filter(owner=intruder).count() == 0


def test_mutations_are_post_only(client):
    user, char = _member()
    client.force_login(user)
    for path in ("/public-profile/save/", "/public-profile/rotate/",
                 "/public-profile/toggle/", "/public-profile/delete/"):
        assert client.get(path).status_code == 405


def test_publishing_for_a_relinked_character_transfers_ownership():
    old_owner, char = _member()
    _publish(old_owner, char)
    new_owner, _nc = _member("eve:3001", main_id=3001, name="New Human")
    char.user = new_owner
    char.save(update_fields=["user"])
    p = _publish(new_owner, char, sections=["skills"])
    assert p.owner == new_owner


# --------------------------------------------------------------------------- #
#  Lifecycle hooks
# --------------------------------------------------------------------------- #
def test_unlink_retires_the_profile():
    from apps.sso import linking

    user, char = _member()
    alt = EveCharacter.objects.create(character_id=2002, user=user, name="Alt", is_corp_member=True)
    p = _publish(user, alt)
    linking.unlink(user, alt)
    assert not PublicProfile.objects.filter(pk=p.pk).exists()
    assert PublicProfile.objects.filter(character=char).count() == 0


def test_delete_my_data_removes_publications():
    from apps.identity.services import delete_user_data

    user, char = _member()
    p = _publish(user, char)
    summary = delete_user_data(user)
    assert summary["deleted"]["public_profiles"] == 1
    assert not PublicProfile.objects.filter(pk=p.pk).exists()


# --------------------------------------------------------------------------- #
#  Discoverability + nav
# --------------------------------------------------------------------------- #
def test_nav_and_character_page_link_to_the_feature(client):
    user, char = _member()
    client.force_login(user)
    dash = client.get("/dashboard/").content.decode()
    assert "/public-profile/" in dash and "Public Profile" in dash
    page = client.get(f"/characters/{char.character_id}/").content.decode()
    assert f"/public-profile/?c={char.character_id}" in page and "Share publicly" in page
    _publish(user, char)
    page = client.get(f"/characters/{char.character_id}/").content.decode()
    assert "Public profile" in page
    set_disabled(["public_profiles"])
    dash = client.get("/dashboard/").content.decode()
    assert "/public-profile/" not in dash


def test_privacy_page_explains_sharing(client):
    user, _char = _member()
    client.force_login(user)
    body = client.get("/privacy/").content.decode()
    assert "Sharing outside the corp" in body and "/public-profile/" in body


def test_flyable_ships_helper():
    from apps.skills.flyable import flyable_ships

    _skill_types()
    user, char = _member()
    snap = _snapshot(char, {"3330": {"trained_level": 1, "sp": 250}})
    groups = flyable_ships(snap)
    assert [g["group"] for g in groups] == ["Frigate"]
    assert [s["name"] for s in groups[0]["ships"]] == ["Kestrel"]
    assert flyable_ships(None) == []
    snap.skills = {"3330": {"trained_level": 0, "sp": 0}}
    assert flyable_ships(snap) == []


# --------------------------------------------------------------------------- #
#  Implants ride along with the attribute import (the section depends on it)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, data):
        self.data = data


class _AttrClient:
    def __init__(self, *, implants=None, fail_implants=False):
        self.implants = implants or []
        self.fail_implants = fail_implants

    def get(self, path, token=None):
        if path.endswith("/implants/"):
            if self.fail_implants:
                raise RuntimeError("scope missing")
            return _Resp(self.implants)
        return _Resp({"intelligence": 27, "memory": 21, "perception": 23, "willpower": 24,
                      "charisma": 19})


def test_attribute_import_stores_implants(monkeypatch):
    from apps.characters.services import import_character_attributes

    monkeypatch.setattr("apps.characters.services.get_valid_access_token", lambda ch, sc: "tok")
    _user, char = _member()
    attrs = import_character_attributes(char, client=_AttrClient(implants=[33516, 10228]))
    assert attrs.intelligence == 27
    assert attrs.implants == [10228, 33516]
    # A failed implant read never fails the import and never clears what was known.
    attrs = import_character_attributes(char, client=_AttrClient(fail_implants=True))
    attrs.refresh_from_db()
    assert attrs.willpower == 24 and attrs.implants == [10228, 33516]
