"""Director "view-as" impersonation.

Covers the security-critical invariants: who may impersonate whom, the request.user swap,
read-only enforcement, per-request re-validation (demotion / promotion), auto-expiry, the
audit trail, and the exit path.
"""
from __future__ import annotations

import time

import pytest

from apps.admin_audit.models import AuditLog
from apps.impersonation import policy
from apps.impersonation.models import ImpersonationSession
from apps.impersonation.policy import can_impersonate
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, name, *roles, is_superuser=False, main_char_id=None):
    user = django_user_model.objects.create(
        username=name, first_name=name.title(), is_superuser=is_superuser
    )
    for r in roles:
        from apps.identity.models import RoleAssignment
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    if main_char_id:
        # is_corp_director: since LP-4 the app's Director role is only exercisable from a pilot who
        # holds the in-game Director role, so a director fixture needs the seat that proves it.
        EveCharacter.objects.create(
            character_id=main_char_id, user=user, name=name.title(),
            is_main=True, is_corp_member=True,
            is_corp_director=rbac.ROLE_DIRECTOR in roles,
        )
    return user


# --- policy.can_impersonate matrix -------------------------------------------
@pytest.mark.django_db
def test_can_impersonate_rank_matrix(django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    member = _user(django_user_model, "mem", rbac.ROLE_MEMBER)
    other_director = _user(django_user_model, "dir2", rbac.ROLE_DIRECTOR)
    admin = _user(django_user_model, "admin", is_superuser=True)

    # A director can view anyone strictly below their rank.
    assert can_impersonate(director, member) is True
    assert can_impersonate(director, officer) is True
    # ...but never a peer, a higher rank, a superuser, or themselves.
    assert can_impersonate(director, other_director) is False
    assert can_impersonate(director, admin) is False
    assert can_impersonate(director, director) is False
    # A member/officer can never impersonate at all.
    assert can_impersonate(member, member) is False
    assert can_impersonate(officer, member) is False
    # An admin (rank 40) can additionally view a director (rank 30) — but not another admin.
    assert can_impersonate(admin, director) is True
    assert can_impersonate(admin, member) is True
    another_admin = _user(django_user_model, "admin2", is_superuser=True)
    assert can_impersonate(admin, another_admin) is False


@pytest.mark.django_db
def test_can_impersonate_inactive_target_blocked(django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    member = _user(django_user_model, "mem", rbac.ROLE_MEMBER)
    member.is_active = False
    member.save(update_fields=["is_active"])
    assert can_impersonate(director, member) is False


# --- Entry gating ------------------------------------------------------------
@pytest.mark.django_db
def test_start_requires_director(client, django_user_model):
    member = _user(django_user_model, "mem", rbac.ROLE_MEMBER)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER)

    # Anonymous → redirected to login, no session created.
    assert client.post(f"/impersonation/start/{target.id}/").status_code in (302, 403)
    assert not ImpersonationSession.objects.exists()

    # A plain member cannot start impersonation.
    client.force_login(member)
    assert client.post(f"/impersonation/start/{target.id}/").status_code == 403
    assert not ImpersonationSession.objects.exists()


@pytest.mark.django_db
def test_director_cannot_impersonate_peer_or_up(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    peer = _user(django_user_model, "dir2", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    assert client.post(f"/impersonation/start/{peer.id}/").status_code == 403
    assert not ImpersonationSession.objects.exists()


# --- The swap ----------------------------------------------------------------
@pytest.mark.django_db
def test_start_swaps_request_user_and_audits(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR, main_char_id=2001)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)

    # Before: the director can reach a director-only console page.
    assert client.get("/ops/admin/members/").status_code == 200

    resp = client.post(f"/impersonation/start/{target.id}/")
    assert resp.status_code == 302
    session = ImpersonationSession.objects.get()
    assert session.actor_id == director.id and session.target_id == target.id
    assert session.ended_at is None
    assert AuditLog.objects.filter(action="impersonation.start", target_id=str(target.id)).exists()
    assert client.session.get(policy.SESSION_TARGET_KEY) == target.id

    # After: every request is the pilot. A director-only page now 403s (the pilot is a member).
    assert client.get("/ops/admin/members/").status_code == 403

    # A member-accessible page renders as the pilot, and the banner exposes the Exit control.
    page = client.get("/auth/eve/scopes/")
    assert page.status_code == 200
    assert page.wsgi_request.user.id == target.id
    assert page.wsgi_request.is_impersonating is True
    assert page.wsgi_request.impersonator.id == director.id
    body = page.content.decode()
    assert "Viewing as" in body
    assert "Exit — return to your account" in body


# --- Read-only enforcement ---------------------------------------------------
@pytest.mark.django_db
def test_impersonation_is_read_only(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")

    # Any unsafe method to a normal endpoint is refused before the view runs.
    resp = client.post("/dashboard/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/")
    assert AuditLog.objects.filter(action="impersonation.write_blocked").exists()
    # The session is NOT ended by a blocked write — it stays active.
    assert client.session.get(policy.SESSION_TARGET_KEY) == target.id


@pytest.mark.django_db
def test_get_served_identity_flows_are_blocked(client, django_user_model):
    """OAuth/SSO callbacks mutate the CURRENT account on GET — under a swapped identity they
    would bind the director's character/Discord to the pilot. They must be refused even on GET,
    while ordinary read pages stay viewable."""
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")

    for path in ("/auth/eve/login/", "/auth/eve/callback/", "/comms/discord/callback/"):
        resp = client.get(path)
        assert resp.status_code == 302, f"{path} should be blocked while impersonating"
        assert resp.headers["Location"].startswith("/")
    assert AuditLog.objects.filter(action="impersonation.write_blocked").count() >= 3
    # The session stays active (blocking a request never ends impersonation)...
    assert client.session.get(policy.SESSION_TARGET_KEY) == target.id
    # ...and a read-only page (the pilot's scopes view) is still reachable for troubleshooting.
    assert client.get("/auth/eve/scopes/").status_code == 200


@pytest.mark.django_db
def test_cannot_nest_impersonation(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER)
    other = _user(django_user_model, "tgt2", rbac.ROLE_MEMBER)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")
    # request.user is now the member, so starting another is role-blocked; no 2nd row.
    assert client.post(f"/impersonation/start/{other.id}/").status_code == 403
    assert ImpersonationSession.objects.count() == 1


# --- Exit --------------------------------------------------------------------
@pytest.mark.django_db
def test_stop_returns_to_director(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")

    resp = client.post("/impersonation/stop/")
    assert resp.status_code == 302
    session = ImpersonationSession.objects.get()
    assert session.ended_at is not None
    assert session.end_reason == "manual"
    assert AuditLog.objects.filter(action="impersonation.end").exists()
    assert client.session.get(policy.SESSION_TARGET_KEY) is None

    # Back to the director: the director-only page is reachable again.
    assert client.get("/ops/admin/members/").status_code == 200


# --- Per-request re-validation ----------------------------------------------
@pytest.mark.django_db
def test_demoting_the_director_mid_session_ends_it(client, django_user_model):
    from apps.identity.models import RoleAssignment
    director = _user(django_user_model, "dir", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")
    assert client.session.get(policy.SESSION_TARGET_KEY) == target.id

    # Strip the director role; the very next request must drop the impersonation.
    RoleAssignment.objects.filter(user=director, role__key=rbac.ROLE_DIRECTOR).delete()
    resp = client.get("/auth/eve/scopes/")
    assert resp.wsgi_request.is_impersonating is False
    assert resp.wsgi_request.user.id == director.id
    session = ImpersonationSession.objects.get()
    assert session.end_reason == "actor_invalid"
    assert client.session.get(policy.SESSION_TARGET_KEY) is None


@pytest.mark.django_db
def test_promoting_the_target_mid_session_ends_it(client, django_user_model):
    from apps.identity.models import RoleAssignment
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR, main_char_id=2001)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")

    # Promote the target to the actor's rank — no longer strictly below → session ends.
    RoleAssignment.objects.create(user=target, role=ensure_role(rbac.ROLE_DIRECTOR))
    resp = client.get("/auth/eve/scopes/")
    assert resp.wsgi_request.is_impersonating is False
    assert ImpersonationSession.objects.get().end_reason == "target_invalid"


@pytest.mark.django_db
def test_auto_expiry(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR, main_char_id=2001)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")

    # Backdate the start beyond the cap.
    s = client.session
    s[policy.SESSION_STARTED_KEY] = int(time.time()) - 10 ** 7
    s.save()

    resp = client.get("/auth/eve/scopes/")
    assert resp.wsgi_request.is_impersonating is False
    assert ImpersonationSession.objects.get().end_reason == "expired"
    assert client.session.get(policy.SESSION_TARGET_KEY) is None


@pytest.mark.django_db
def test_logout_while_impersonating_closes_session(client, django_user_model):
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR, main_char_id=2001)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)
    client.force_login(director)
    client.post(f"/impersonation/start/{target.id}/")

    assert client.post("/auth/eve/logout/").status_code == 302
    session = ImpersonationSession.objects.get()
    assert session.ended_at is not None
    assert session.end_reason == "logout"


# --- Defence-in-depth: identity-mutation sinks refuse under impersonation -----
# (The middleware blocks these routes before the view runs, so they're exercised directly.)
@pytest.mark.django_db
def test_sso_views_refuse_while_impersonating(rf):
    from apps.sso.views import callback_view, login_view

    for view in (login_view, callback_view):
        request = rf.get("/auth/eve/x/")
        request.is_impersonating = True
        assert view(request).status_code == 400


@pytest.mark.django_db
def test_discord_callback_refuses_while_impersonating(rf, django_user_model):
    from django.contrib.messages.storage.fallback import FallbackStorage

    from apps.comms_access.models import CommsAccount
    from apps.comms_access.views import discord_callback

    user = _user(django_user_model, "tgt", rbac.ROLE_MEMBER)
    request = rf.get("/comms/discord/callback/?code=x&state=y")
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    request.is_impersonating = True
    resp = discord_callback(request)
    assert resp.status_code == 302  # bounced to the connect page, refused
    assert not CommsAccount.objects.exists()  # and crucially: no identity was rebound


# --- Prefetch-awareness (no N+1 when ranking a list of users) -----------------
@pytest.mark.django_db
def test_max_role_rank_uses_prefetch_cache(django_user_model, django_assert_num_queries):
    for i in range(5):
        _user(django_user_model, f"m{i}", rbac.ROLE_MEMBER)
    users = list(django_user_model.objects.prefetch_related("role_assignments__role"))
    # Ranking every already-prefetched user must add ZERO further queries.
    with django_assert_num_queries(0):
        ranks = [u.max_role_rank() for u in users]
    assert ranks and all(r == rbac.ROLE_RANK[rbac.ROLE_MEMBER] for r in ranks)


# --- Log view ----------------------------------------------------------------
@pytest.mark.django_db
def test_log_view_is_director_gated(client, django_user_model):
    member = _user(django_user_model, "mem", rbac.ROLE_MEMBER)
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR)

    client.force_login(member)
    assert client.get("/impersonation/log/").status_code == 403

    client.force_login(director)
    assert client.get("/impersonation/log/").status_code == 200


# --- Read-only is not enough: bearer tokens must not be readable either --------
@pytest.mark.django_db
def test_pending_verify_code_is_hidden_while_impersonating(client, django_user_model, settings):
    """A pilot's pending Telegram verify code must never render to a viewing-as director.

    The HTTP write-block does not protect it: the code is redeemed out-of-band by
    messaging the bot, so a director who could read it would bind their own chat id to
    the pilot's channel and start receiving that pilot's DMs.
    """
    settings.PINGBOARD_TELEGRAM_BOT_USERNAME = "forca_test_bot"
    director = _user(django_user_model, "dir", rbac.ROLE_DIRECTOR, main_char_id=2001)
    target = _user(django_user_model, "tgt", rbac.ROLE_MEMBER, main_char_id=1001)

    # The pilot has started a Telegram link and has a live, unredeemed code.
    from apps.pingboard import linking
    row = linking.start_link(target, "telegram", handle="@pilot")
    code = row.verify_code
    assert code

    # The pilot themselves sees their own code (the deep link is how linking works).
    client.force_login(target)
    body = client.get("/pingboard/channels/").content.decode()
    assert code in body

    # The director viewing-as the pilot must not.
    client.force_login(director)
    assert client.post(f"/impersonation/start/{target.id}/").status_code == 302
    page = client.get("/pingboard/channels/")
    assert page.status_code == 200
    assert page.wsgi_request.is_impersonating is True
    body = page.content.decode()
    assert code not in body
    assert "t.me/forca_test_bot" not in body

    # The stored row is untouched — blanking happens in memory only, so the pilot's
    # own link keeps working after the director exits.
    row.refresh_from_db()
    assert row.verify_code == code
