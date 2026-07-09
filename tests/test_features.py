"""Leader-configurable feature toggles: default-on, nav hiding, view 404, admin."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac
from core.features import (
    disabled_set,
    enabled_map,
    feature_enabled,
    feature_for_view,
    set_disabled,
)

# Every toggle added so leadership can turn each restricted feature on/off. All
# ship default-ENABLED, so wiring them changes nothing until a leader flips one.
NEW_FEATURES = [
    "structures", "corp_contracts", "finance", "briefing", "contributions",
    "command_intel", "readiness", "recommendations", "recruitment",
]


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.mark.django_db
def test_default_is_everything_enabled():
    assert feature_enabled("market") is True
    assert feature_enabled("killboard") is True
    assert all(enabled_map().values())
    assert disabled_set() == set()


@pytest.mark.django_db
def test_disabling_persists_and_busts_cache():
    feature_enabled("market")  # warm the cache
    set_disabled(["market"])
    assert feature_enabled("market") is False     # cache was invalidated
    assert feature_enabled("mining") is True
    m = enabled_map()
    assert m["market"] is False and m["mining"] is True


@pytest.mark.django_db
def test_unknown_keys_are_ignored():
    stored = set_disabled(["market", "not_a_feature"])
    assert stored == {"market"}
    assert disabled_set() == {"market"}


@pytest.mark.django_db
def test_nav_hides_disabled_feature(client, django_user_model, sde):
    member = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)

    html = client.get("/dashboard/").content.decode()
    assert 'href="/market/"' in html            # on by default

    set_disabled(["market"])
    html = client.get("/dashboard/").content.decode()
    assert 'href="/market/"' not in html        # hidden once disabled
    assert 'href="/mining/me/"' in html          # other features unaffected (member's My mining link)


@pytest.mark.django_db
def test_disabled_feature_view_returns_404(client, django_user_model, sde):
    member = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)

    assert client.get("/market/").status_code == 200      # enabled by default
    set_disabled(["market"])
    assert client.get("/market/").status_code == 404      # gated by middleware
    # A different (member-accessible) feature still works.
    assert client.get("/killboard/").status_code == 200


@pytest.mark.django_db
def test_intel_split_across_apps_is_gated_together(client, django_user_model, sde):
    # 'intel' spans killboard watchlists + navigation roaming/gatecamp.
    member = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)
    set_disabled(["intel"])
    assert client.get("/killboard/intel/").status_code == 404
    assert client.get("/navigation/roaming/").status_code == 404
    # The killboard board itself (the 'killboard' feature) is still on.
    assert client.get("/killboard/").status_code == 200


@pytest.mark.django_db
def test_features_page_is_director_only_and_saves(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    assert client.get("/ops/admin/features/").status_code == 403

    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    assert client.get("/ops/admin/features/").status_code == 200
    # Submit with 'market' unchecked (everything else checked) → market disabled.
    from core.features import FEATURES

    checked = [f.key for f in FEATURES if f.key != "market"]
    resp = client.post("/ops/admin/features/", {"feature": checked})
    assert resp.status_code == 302
    assert feature_enabled("market") is False
    assert feature_enabled("mining") is True


@pytest.mark.django_db
def test_features_page_sets_member_service_audience(client, django_user_model, sde):
    # The one-stop page also controls who can see each member service (off / corp /
    # alliance / public) — leaders shouldn't have to visit three separate pages.
    from apps.logistics.services import current_audience as freight_audience
    from apps.store.services import current_audience as store_audience

    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))

    # The selectors render with the current audience pre-selected.
    html = client.get("/ops/admin/features/").content.decode()
    assert 'name="audience:freight"' in html
    assert 'name="audience:buyback"' in html
    assert 'name="audience:store"' in html

    from core.features import FEATURES

    checked = [f.key for f in FEATURES]  # keep every feature on
    resp = client.post("/ops/admin/features/", {
        "feature": checked,
        "audience:freight": "corp",
        "audience:buyback": "disabled",
        "audience:store": "alliance",
    })
    assert resp.status_code == 302
    assert freight_audience() == "corp"        # narrowed to corp-only
    assert store_audience() == "alliance"      # cache invalidated, new value visible

    # A tampered/unknown audience value is ignored, not persisted.
    client.post("/ops/admin/features/", {"feature": checked, "audience:freight": "bogus"})
    assert freight_audience() == "corp"


# --- newly-added toggles (every restricted feature is now switchable) --------
@pytest.mark.django_db
def test_new_toggles_default_enabled():
    cache.clear()
    for key in NEW_FEATURES:
        assert feature_enabled(key) is True, f"{key} should default enabled"
    assert all(enabled_map().values())


def test_feature_for_view_map_is_correct():
    # Namespace-mapped strategic surfaces.
    assert feature_for_view("command_intel", "overview") == "command_intel"
    assert feature_for_view("command_intel", "reports") == "command_intel"
    assert feature_for_view("readiness", "dashboard") == "readiness"
    assert feature_for_view("readiness", "me") == "readiness"
    assert feature_for_view("recommendations", "officer") == "recommendations"
    assert feature_for_view("recommendations", "personal") == "recommendations"
    assert feature_for_view("recruitment", "list") == "recruitment"
    assert feature_for_view("recruitment", "oauth_callback") == "recruitment"
    # The command_intel pilot slice keeps its own key even though the namespace maps.
    assert feature_for_view("command_intel", "me") == "command_intel_pilot"
    assert feature_for_view("command_intel", "directive_action") == "command_intel_pilot"
    # Per-view gates inside namespaces that must stay partly ungated.
    assert feature_for_view("corporation", "structures") == "structures"
    assert feature_for_view("corporation", "finance") == "finance"
    assert feature_for_view("corporation", "income") == "finance"
    assert feature_for_view("logistics", "corp_contracts") == "corp_contracts"
    # The merged Daily Briefing spans three keys and gates itself in-view, so it
    # must NOT be middleware-gated (a single key here would 404 the whole page).
    assert feature_for_view("pilots", "briefing") is None
    assert feature_for_view("pilots", "contributions") == "contributions"
    assert feature_for_view("pilots", "toggle_recognition") == "contributions"
    # These must NEVER be gated: roster/finance-plumbing siblings + the freight
    # service (audience-gated, not a Feature) + account/core.
    assert feature_for_view("corporation", "roster") is None
    assert feature_for_view("logistics", "calculator") is None
    assert feature_for_view("pilots", "hall_of_fame") == "hall_of_fame"


@pytest.mark.django_db
def test_each_new_toggle_404s_its_view_when_disabled(client, django_user_model, sde):
    # A director passes the member/officer gates, so one login exercises every surface.
    # When a feature is off the FeatureGateMiddleware 404s its view before it runs.
    director = _user(django_user_model, "gatekeeper", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    cases = {
        "command_intel": "/command/",
        "readiness": "/readiness/",
        "recommendations": "/recommendations/officer/",
        "recruitment": "/recruitment/",
        "structures": "/roster/structures/",
        "finance": "/roster/finance/",
        "corp_contracts": "/freight/corp-contracts/",
        "contributions": "/pilots/contributions/",
    }
    for key, url in cases.items():
        set_disabled([key])
        assert client.get(url).status_code == 404, f"{key} did not gate {url}"
        set_disabled([])  # re-enable before the next case


@pytest.mark.django_db
def test_absorbed_briefing_stub_keeps_union_gating(client, django_user_model, sde):
    # /pilots/briefing/ is a redirect into /dashboard/ now, but its old union
    # off-switch survives: redirect while ANY of its three old section features
    # is on, 404 only when leadership turns all three off. The Command Center
    # itself is always-on.
    keys = {"briefing", "command_intel_pilot", "recommendations"}
    member = _user(django_user_model, "briefing-union", rbac.ROLE_MEMBER)
    client.force_login(member)
    for lone in sorted(keys):
        set_disabled(sorted(keys - {lone}))
        assert client.get("/pilots/briefing/").status_code == 302, f"stub gone with only {lone} on"
        assert client.get("/dashboard/").status_code == 200
    set_disabled(sorted(keys))
    assert client.get("/pilots/briefing/").status_code == 404
    assert client.get("/dashboard/").status_code == 200  # the home never 404s
    set_disabled([])
    # The other absorbed URLs redirect too, each still gated by its old key.
    assert client.get("/command/me/").status_code == 302
    assert client.get("/recommendations/mine/").status_code == 302
    assert client.get("/readiness/me/").status_code == 302
    set_disabled(["command_intel_pilot"])
    assert client.get("/command/me/").status_code == 404
    set_disabled(["recommendations"])
    assert client.get("/recommendations/mine/").status_code == 404
    set_disabled(["readiness"])
    assert client.get("/readiness/me/").status_code == 404
    set_disabled([])


@pytest.mark.django_db
def test_command_intel_officer_surface_and_pilot_slice_toggle_independently(
        client, django_user_model, sde):
    cache.clear()
    officer = _user(django_user_model, "ci-officer", rbac.ROLE_OFFICER)
    client.force_login(officer)
    # Disabling the officer surface must NOT gate the pilot quest log (own key).
    set_disabled(["command_intel"])
    assert client.get("/command/").status_code == 404
    assert feature_enabled("command_intel_pilot") is True
    # …and vice-versa: disabling only the pilot slice leaves the officer surface up.
    set_disabled(["command_intel_pilot"])
    assert client.get("/command/").status_code != 404
    set_disabled([])


@pytest.mark.django_db
def test_per_view_gate_keeps_the_rest_of_its_namespace_reachable(client, django_user_model, sde):
    # Turning off Structures + Corp finance must not 404 the Member Roster, and
    # turning off Corp contracts must not 404 the freight calculator (own audience gate).
    director = _user(django_user_model, "ns-keeper", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    set_disabled(["structures", "finance", "corp_contracts"])
    assert client.get("/roster/structures/").status_code == 404
    assert client.get("/roster/finance/").status_code == 404
    assert client.get("/freight/corp-contracts/").status_code == 404
    assert client.get("/roster/").status_code != 404  # the roster itself is never gated
    set_disabled([])


@pytest.mark.django_db
def test_new_features_render_on_services_page_without_duplicate_group_headers(
        client, django_user_model, sde):
    cache.clear()
    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    html = client.get("/ops/admin/features/").content.decode()
    for label in ("Command Intelligence", "Readiness platform", "Recommendations &amp; alerts",
                  "Recruitment", "Structures", "Corp contracts", "Corp finance",
                  "Daily briefing", "Contribution ledger"):
        assert label in html, f"{label!r} missing from Services & features page"
    # groupby needs contiguity — each group header must appear exactly once.
    for header in (">Fleet &amp; combat</h2>", ">Industry &amp; economy</h2>",
                   ">Pilot tools</h2>", ">Command &amp; readiness</h2>", ">Leadership</h2>"):
        assert html.count(header) == 1, f"duplicate/missing group header {header!r}"


@pytest.mark.django_db
def test_nav_hides_disabled_command_intel_group_but_keeps_pilot_orders(client, django_user_model, sde):
    cache.clear()
    officer = _user(django_user_model, "nav-officer", rbac.ROLE_OFFICER)
    client.force_login(officer)
    html = client.get("/dashboard/").content.decode()
    assert ">Command Intelligence</span>" in html and "/command/reports/" in html

    set_disabled(["command_intel"])
    html = client.get("/dashboard/").content.decode()
    assert ">Command Intelligence</span>" not in html   # whole officer group hidden
    assert "/command/reports/" not in html
    assert "> Dashboard</a>" in html                     # the pilot home stays
    set_disabled([])


@pytest.mark.django_db
def test_disabling_features_leaves_no_dead_links_on_always_on_pages(client, django_user_model, sde):
    # Regression (reviewer catch): always-on pages that are NOT themselves gated by
    # these features must not hard-link a view the middleware now 404s. The member
    # dashboard and the officer "← Command" back-links were the worst offenders.
    officer = _user(django_user_model, "deadlink", rbac.ROLE_OFFICER)
    client.force_login(officer)
    set_disabled(["recommendations", "contributions", "readiness"])
    # The always-on member/officer home must still render (no 500) and carry no link
    # to a now-gated view.
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    html = resp.content.decode()
    for dead in ('href="/recommendations/', 'href="/pilots/contributions/', 'href="/readiness/'):
        assert dead not in html, f"dead link {dead} left on the dashboard when its feature is off"
    # The privacy page's recognition toggle (posts to a contributions-gated view) is gone too.
    priv = client.get("/privacy/")
    assert priv.status_code == 200
    assert "/pilots/recognition/toggle/" not in priv.content.decode()
    set_disabled([])
