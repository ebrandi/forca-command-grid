"""Knowledge base: safe rendering, live embeds, visibility tiers."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.kb.models import KbPage
from apps.kb.render import render_markdown
from apps.kb.services import make_resolver
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300
RIFTER = 587


def test_render_escapes_html_injection():
    # A script tag in page content must be rendered inert.
    out = render_markdown("Hello <script>alert(1)</script> **world**")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>world</strong>" in out


def test_render_only_allows_safe_links():
    out = render_markdown("[ok](https://example.com) [bad](javascript:alert(1))")
    assert '<a href="https://example.com"' in out   # safe link rendered
    assert 'href="javascript:' not in out           # unsafe scheme never becomes an href


@pytest.mark.django_db
def test_readiness_embed_is_viewer_scoped(django_user_model, sde):
    cat = DoctrineCategory.objects.create(key="t", label="Tackle")
    d = Doctrine.objects.create(name="Tackle", category=cat)
    fit = DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=3, optimal_level=3)

    user = django_user_model.objects.create(username="eve:1")
    ch = EveCharacter.objects.create(
        character_id=1, user=user, name="P", is_main=True, is_corp_member=True
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": 5, "sp": 0}}
    )

    out = render_markdown("Status: {{readiness:doctrine=Tackle}}", make_resolver(user))
    assert "You can fly Tackle" in out


@pytest.mark.django_db
def test_officer_page_hidden_from_member(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    KbPage.objects.create(
        slug="secret", title="Deploy SOP", visibility=KbPage.Visibility.OFFICER, body_md="x"
    )
    client.force_login(member)
    assert client.get("/kb/secret/").status_code == 403
    # And it isn't listed.
    assert b"Deploy SOP" not in client.get("/kb/").content


@pytest.mark.django_db
def test_officer_can_author(client, django_user_model, sde):
    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    resp = client.post(
        "/kb/new/save/",
        {"title": "Logi Guide", "body_md": "# Hi", "visibility": "member", "category": "Roles"},
    )
    assert resp.status_code == 302
    page = KbPage.objects.get(title="Logi Guide")
    assert page.revisions.count() == 1
    # Member create is blocked.
    client.force_login(django_user_model.objects.create(username="m2"))
    assert client.post("/kb/new/save/", {"title": "x"}).status_code in (302, 403)
