"""REC-KB-1 — public knowledge surface (anonymous + recruit access to the public tier)."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.kb.models import KbPage
from apps.kb.services import make_resolver
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _page(slug, vis, title=None):
    return KbPage.objects.create(slug=slug, title=title or slug.title(),
                                 visibility=vis, body_md="# Hello\n\nWho we are.")


# --- anonymous access --------------------------------------------------------
def test_anonymous_reads_public_page(client):
    _page("who-we-are", KbPage.Visibility.PUBLIC, "Who we are")
    resp = client.get("/kb/who-we-are/")
    assert resp.status_code == 200
    assert b"Who we are" in resp.content


def test_anonymous_blocked_from_member_and_officer_pages(client):
    # 404 (not 403) for anonymous, so a gated page's existence isn't confirmed to prospects.
    _page("member-sop", KbPage.Visibility.MEMBER)
    _page("officer-sop", KbPage.Visibility.OFFICER)
    assert client.get("/kb/member-sop/").status_code == 404
    assert client.get("/kb/officer-sop/").status_code == 404


def test_authed_member_gets_403_on_officer_page(client, django_user_model):
    # a logged-in member still gets 403 (they may know a higher tier exists)
    member = django_user_model.objects.create(username="m-403")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    _page("officer-only", KbPage.Visibility.OFFICER)
    client.force_login(member)
    assert client.get("/kb/officer-only/").status_code == 403


def test_anonymous_list_shows_only_public(client):
    _page("public-a", KbPage.Visibility.PUBLIC, "Public A")
    _page("member-b", KbPage.Visibility.MEMBER, "Member B")
    content = client.get("/kb/").content
    assert client.get("/kb/").status_code == 200
    assert b"Public A" in content
    assert b"Member B" not in content


# --- logged-in recruit (non-member) ------------------------------------------
def test_recruit_reaches_public_kb(client, django_user_model):
    # a logged-in user with no member role is a recruit; /kb is a recruit-allowed prefix
    recruit = django_user_model.objects.create(username="recruit")
    _page("apply", KbPage.Visibility.PUBLIC, "How to apply")
    client.force_login(recruit)
    resp = client.get("/kb/apply/")
    assert resp.status_code == 200
    assert b"How to apply" in resp.content


def test_member_still_sees_member_pages(client, django_user_model):
    member = django_user_model.objects.create(username="member")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    _page("member-guide", KbPage.Visibility.MEMBER, "Member Guide")
    client.force_login(member)
    assert client.get("/kb/member-guide/").status_code == 200


# --- embed resolver anonymous guard ------------------------------------------
def test_anonymous_embed_resolver_does_not_crash():
    resolve = make_resolver(None)  # anonymous / no user
    out = resolve("readiness", "doctrine=Foo")
    assert out is not None and "log in" in out
    assert resolve("my-srp", "") is not None


# --- landing surface ---------------------------------------------------------
def test_landing_lists_public_kb(client):
    _page("about-us", KbPage.Visibility.PUBLIC, "About Us")
    _page("secret", KbPage.Visibility.MEMBER, "Secret Ops")
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"About Us" in resp.content
    assert b"Secret Ops" not in resp.content
