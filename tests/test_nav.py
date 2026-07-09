"""Sidebar active-state highlighting.

Stockpile and Logistics share the 'stockpile' namespace, so the active highlight
must key on the URL name, not just the namespace (regression: Logistics lit up
Stockpile). My Actions / Command share 'recommendations' the same way.
"""
from __future__ import annotations

import re

import pytest

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

ACTIVE = "bg-panel2 text-gold"


def _link_classes(html: str, icon: str) -> str:
    """The class attribute of the nav anchor carrying the given icon."""
    m = re.search(r'class="(navlink[^"]*)"><svg class="navicon"><use href="#' + re.escape(icon) + '"', html)
    assert m, f"nav link for #{icon} not found"
    return m.group(1)


def _member(django_user_model, role=rbac.ROLE_MEMBER):
    user, _ = django_user_model.objects.get_or_create(username=f"nav-{role}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_logistics_highlights_logistics_not_stockpile(client, django_user_model, sde):
    client.force_login(_member(django_user_model))
    html = client.get("/stockpile/logistics/").content.decode()
    assert ACTIVE in _link_classes(html, "i-truck")   # Logistics active
    assert ACTIVE not in _link_classes(html, "i-box")  # Stockpile not active


@pytest.mark.django_db
def test_stockpile_highlights_stockpile_not_logistics(client, django_user_model, sde):
    client.force_login(_member(django_user_model))
    html = client.get("/stockpile/").content.decode()
    assert ACTIVE in _link_classes(html, "i-box")       # Stockpile active
    assert ACTIVE not in _link_classes(html, "i-truck")  # Logistics not active


@pytest.mark.django_db
def test_command_highlights_command_not_my_actions(client, django_user_model, sde):
    client.force_login(_member(django_user_model, role=rbac.ROLE_OFFICER))
    html = client.get("/recommendations/officer/").content.decode()
    assert ACTIVE in _link_classes(html, "i-command")  # Command active
    assert ACTIVE not in _link_classes(html, "i-bolt")  # My Actions not active


@pytest.mark.django_db
def test_skill_sheet_is_discoverable_from_dashboard(client, django_user_model, sde):
    # A member with a linked character must have an obvious way to see skills:
    # the "My Skills" nav link and a dashboard link to their skill sheet.
    from apps.sso.models import EveCharacter

    user = _member(django_user_model)
    EveCharacter.objects.create(
        character_id=4242, user=user, name="Pilot", is_main=True, is_corp_member=True
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "/characters/4242/" in html  # links to the skill sheet
    assert "My Skills" in html                      # nav entry present
    assert "My skills &amp; readiness" in html      # hero affordance


def _set_home_alliance(settings):
    from apps.corporation.models import EveAlliance, EveCorporation

    settings.FORCA_HOME_CORP_ID = 98000001
    alliance = EveAlliance.objects.create(alliance_id=99000001, name="Home Alliance")
    EveCorporation.objects.create(corporation_id=98000001, name="Home", alliance=alliance)


@pytest.mark.django_db
def test_registered_alliance_pilot_sees_alliance_services(client, django_user_model, settings, sde):
    # A logged-in pilot in the home alliance (but not the home corp) gets the
    # alliance-facing services in their sidebar, even though they're not a member.
    from django.core.cache import cache

    from apps.sso.models import EveCharacter
    from core.features import set_feature_audiences

    cache.clear()
    _set_home_alliance(settings)
    # Ships & doctrines is its own audience now — open it to the alliance so the pilot
    # gets the doctrines/shipyard nav group.
    set_feature_audiences({"doctrines": "alliance"})
    user = django_user_model.objects.create(username="eve:5500")
    EveCharacter.objects.create(character_id=5500, user=user, name="Ally", alliance_id=99000001)
    client.force_login(user)
    html = client.get("/onboarding/").content.decode()
    assert ">Services</span>" in html
    assert "Corp Store" in html and "Freight Services" in html and "Buyback Services" in html
    assert "/doctrines/ships/" in html and "Shipyard" in html  # RTF ships open to the alliance
    assert ">Build Board" not in html  # the fulfilment board stays corp-only


@pytest.mark.django_db
def test_non_alliance_pilot_has_no_alliance_services(client, django_user_model, settings, sde):
    from django.core.cache import cache

    from apps.sso.models import EveCharacter

    cache.clear()
    _set_home_alliance(settings)
    user = django_user_model.objects.create(username="eve:5501")
    EveCharacter.objects.create(character_id=5501, user=user, name="Outsider", alliance_id=42)
    client.force_login(user)
    html = client.get("/onboarding/").content.decode()
    assert ">Services</span>" not in html


@pytest.mark.django_db
def test_alliance_pilot_hides_corp_only_service(client, django_user_model, settings, sde):
    # When a service is set corp-only, an alliance pilot must not see its link.
    from django.core.cache import cache

    from apps.sso.models import EveCharacter
    from apps.store.models import Audience
    from apps.store.services import active_config, invalidate_audience_cache

    cache.clear()
    _set_home_alliance(settings)
    cfg = active_config()
    cfg.audience = Audience.CORP
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()
    user = django_user_model.objects.create(username="eve:5502")
    EveCharacter.objects.create(character_id=5502, user=user, name="Ally", alliance_id=99000001)
    client.force_login(user)
    html = client.get("/onboarding/").content.decode()
    assert "Corp Store" not in html          # corp-only → hidden from alliance pilot
    assert "Freight Services" in html         # still alliance-visible


@pytest.mark.django_db
def test_nav_is_grouped_by_audience(client, django_user_model, sde):
    # A plain member sees Pilot + the operational sections, but not Leadership.
    client.force_login(_member(django_user_model, role=rbac.ROLE_MEMBER))
    html = client.get("/dashboard/").content.decode()
    assert ">Pilot</span>" in html
    assert ">Ships &amp; Doctrines</span>" in html
    assert ">Community</span>" in html   # renamed from "Community & Intel" (de-overloaded 'Intel')
    assert ">Leadership</span>" not in html

    # An officer gains the single Leadership section, incl. SRP Review and the
    # Admin Console gateway (the separate "Admin" section was merged away).
    client.force_login(_member(django_user_model, role=rbac.ROLE_OFFICER))
    html = client.get("/dashboard/").content.decode()
    assert ">Leadership</span>" in html and "/srp/queue/" in html
    assert "/ops/admin/" in html              # Admin Console reachable by officers now
    assert ">Admin</span>" not in html         # no longer a separate section
    assert "/roster/finance/" not in html      # Corp Finance lives in the hub, not the sidebar

    # After the nav<->hub dedupe Corp Finance has ONE home (the Admin Console hub),
    # so even a director no longer sees it duplicated in the sidebar.
    client.force_login(_member(django_user_model, role=rbac.ROLE_DIRECTOR))
    html = client.get("/dashboard/").content.decode()
    assert "/ops/admin/" in html               # the hub gateway is the way in
    assert "/roster/finance/" not in html      # deduped out of the sidebar
    assert ">Admin</span>" not in html         # still one merged Leadership section


@pytest.mark.django_db
def test_leadership_tools_are_discoverable_in_the_admin_console(client, django_user_model, sde):
    # Deliberate design (nav<->hub dedupe): the operational leadership tools now have
    # ONE home — the Admin Console hub — instead of being duplicated in the sidebar.
    client.force_login(_member(django_user_model, role=rbac.ROLE_DIRECTOR))
    hub = client.get("/ops/admin/").content.decode()
    for url in ("/roster/structures/", "/operations/sov/", "/stockpile/assets/search/",
                "/freight/corp-contracts/", "/ops/admin/compliance/", "/roster/finance/"):
        assert url in hub, f"leadership tool missing from the Admin Console hub: {url}"

    # …and they are no longer duplicated in the sidebar.
    nav = client.get("/dashboard/").content.decode()
    for url in ("/roster/structures/", "/operations/sov/", "/stockpile/assets/search/",
                "/freight/corp-contracts/", "/ops/admin/compliance/", "/roster/finance/"):
        assert url not in nav, f"deduped tool still duplicated in the sidebar: {url}"

    # A plain officer reaches the officer-safe cards via the hub (not the director-only ones).
    client.force_login(_member(django_user_model, role=rbac.ROLE_OFFICER))
    hub = client.get("/ops/admin/").content.decode()
    assert "/operations/sov/" in hub and "/stockpile/assets/search/" in hub
    assert "/roster/income/" not in hub  # Corp Income is director-only


@pytest.mark.django_db
def test_readiness_is_its_own_top_level_officer_group(client, django_user_model, sde):
    # Goal 2: the 6-item Readiness suite was extracted out of the overstuffed
    # Leadership group into its own accordion (a sibling of Command Intelligence),
    # gated by the new features.readiness toggle.
    from django.core.cache import cache

    cache.clear()
    client.force_login(_member(django_user_model, role=rbac.ROLE_OFFICER))
    html = client.get("/dashboard/").content.decode()
    assert 'data-acc-key="readiness"' in html
    assert ">Readiness</span>" in html        # its own group header (eyebrow span)
    assert "/readiness/findings/" in html      # Risk register now lives under it

    # Turning the platform off removes the whole group.
    from core.features import set_disabled

    set_disabled(["readiness"])
    html = client.get("/dashboard/").content.decode()
    assert 'data-acc-key="readiness"' not in html
    assert "/readiness/findings/" not in html
    set_disabled([])


@pytest.mark.django_db
def test_pilot_home_pages_merged_into_the_dashboard(client, django_user_model, sde):
    # All the 'what should I do / how am I doing?' pages merged into the one
    # Dashboard (Command Center) — the absorbed nav labels are gone.
    client.force_login(_member(django_user_model, role=rbac.ROLE_MEMBER))
    html = client.get("/dashboard/").content.decode()
    assert "> Dashboard</a>" in html
    assert "> Daily Briefing</a>" not in html  # absorbed: the merged briefing
    assert "> My Readiness</a>" not in html    # absorbed: readiness pilot page
    assert "Your Orders" not in html           # absorbed: command_intel quest log
    assert "Recommended Actions" not in html    # absorbed: recommendation boards
    assert "> My Actions</a>" not in html       # the old ambiguous nav label stays gone
    assert "/pilots/briefing/" not in html      # no nav link to the redirect stub
    assert 'href="/readiness/me/"' not in html


@pytest.mark.django_db
def test_nav_groups_are_collapsible_accordion(client, django_user_model, sde):
    # Each section is an accordion group app.js can drive; the Shipyard rename
    # has fully replaced the old "Ship Finder" label.
    client.force_login(_member(django_user_model))
    html = client.get("/dashboard/").content.decode()
    assert "data-acc-toggle" in html and "data-acc-body" in html
    assert "data-acc-key=\"ships\"" in html
    assert "Shipyard</a>" in html
    assert "Ship Finder" not in html


@pytest.mark.django_db
def test_no_django_comment_leaks_into_rendered_pages(client, django_user_model, sde):
    """Regression: Django {# #} comments are single-line ONLY; a multi-line one
    renders as literal text. base.html (D8), killboard/_feed.html and
    doctrines/_list_results.html (D5) each had a multi-line {# #} that leaked its
    prose onto every/those page(s). Guard the rendered output for any leak."""
    client.force_login(_member(django_user_model))
    for url in ("/killboard/", "/dashboard/", "/doctrines/"):
        html = client.get(url).content.decode()
        assert "{#" not in html and "#}" not in html, f"leaked Django comment marker on {url}"
        assert "command palette — Cmd/Ctrl-K" not in html  # D8 comment prose (base.html)
        assert "the killfeed fragment" not in html         # D5 comment prose (_feed.html)


@pytest.mark.django_db
def test_command_palette_is_mounted():
    """D8: the Cmd-K command palette + its trigger ship on every page (base.html)."""
    from django.test import Client

    html = Client().get("/killboard/").content.decode()
    assert 'x-data="commandPalette()"' in html          # palette overlay mounted
    assert "$dispatch('cmdk')" in html                  # discoverability trigger
    assert "@keydown.window.meta.k" in html             # Cmd-K shortcut bound
    assert "@keydown.window.ctrl.k" in html             # Ctrl-K shortcut bound
