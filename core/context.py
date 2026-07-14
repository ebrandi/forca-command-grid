"""Template context processors."""
from __future__ import annotations

from django.conf import settings

from core import rbac


def roles(request) -> dict:
    """Expose role booleans and the ACTIVE pilot to templates (nav/portrait)."""
    from core import pilots

    user = getattr(request, "user", None)
    # The pilot the user is currently flying — resolved and ownership-checked once per request
    # by ActivePilotMiddleware (LP-2). Templates that ask "who am I?" mean this, not the
    # account's primary pilot: after a switch, the rail portrait, the mobile avatar and every
    # "my …" link must follow the new pilot or the UI would be quietly lying about identity.
    active = pilots.active_pilot(user) if getattr(user, "is_authenticated", False) else None
    selector = _selector_pilots(user) if active is not None else []
    # Member-service visibility (leadership-controlled audience) for the nav.
    from apps.buyback.services import current_audience as buyback_audience
    from apps.corporation.access import home_corp_name as _home_corp_name
    from apps.logistics.services import current_audience as freight_audience
    from apps.store.services import current_audience as store_audience

    audience = freight_audience()
    bb_audience = buyback_audience()
    st_audience = store_audience()

    is_member = rbac.has_role(user, rbac.ROLE_MEMBER)
    # A registered alliance pilot (in the home corp's alliance) who is not in the
    # home corp itself — they get the alliance-facing services in their sidebar.
    is_alliance = _is_registered_alliance_pilot(user, is_member)

    return {
        "is_member": is_member,
        "is_alliance": is_alliance,
        "is_officer": rbac.has_role(user, rbac.ROLE_OFFICER),
        "is_director": rbac.has_role(user, rbac.ROLE_DIRECTOR),
        # Least-privilege capabilities (4.16): a lateral role holder (recruiter / FC) who
        # is NOT an officer still sees their one surface in the nav.
        "can_recruit": rbac.has_perm(user, rbac.PERM_RECRUITMENT_MANAGE),
        "can_manage_fleet": rbac.has_perm(user, rbac.PERM_FLEET_MANAGE),
        "can_manage_campaigns": rbac.has_perm(user, rbac.PERM_CAMPAIGN_MANAGE),
        # ``main_character`` is retained as the template name because ~30 templates already
        # render it, but it now resolves to the ACTIVE pilot — which is what every one of
        # those call sites always meant by it. ``active_pilot`` is the honest name for new code.
        "main_character": active,
        "active_pilot": active,
        # The selector's roster. Ordered active-first, then primary, then recently used, then
        # alphabetically (core.pilots.ordered_for_selector), each carrying a link_healthy flag.
        "linked_pilots": selector,
        # Home corporation id for branding (its in-game logo is the app mark).
        "home_corp_id": getattr(settings, "FORCA_HOME_CORP_ID", 0),
        # Display name of the app-owning corp (customers contract freight to it).
        "home_corp_name": _home_corp_name(),
        # Audience per service, plus convenience booleans for the nav.
        "freight_audience": audience,
        "buyback_audience": bb_audience,
        "store_audience": st_audience,
        "freight_enabled": audience != "disabled",
        "freight_public": audience == "public",
        "buyback_enabled": bb_audience != "disabled",
        "buyback_public": bb_audience == "public",
        "store_enabled": st_audience != "disabled",
        "store_public": st_audience == "public",
        # Leader-toggled feature flags (default everything on) for the nav.
        "features": _feature_map(user),
    }


def _selector_pilots(user) -> list:
    """The pilot roster for the sidebar selector, each flagged with its ESI health.

    Costs nothing for the single-pilot accounts that are the overwhelming majority: the
    selector does not render at all below two pilots, so we do not go looking for token health
    that nothing will display. Above two, the health of the whole roster is ONE aggregate query
    (``linking.healthy_ids``) — never a query per pilot on every page render.
    """
    from apps.sso.linking import healthy_ids
    from core import pilots

    roster = pilots.ordered_for_selector(user)
    if len(roster) < 2:
        return roster
    healthy = healthy_ids(roster)
    for pilot in roster:
        pilot.link_healthy = pilot.character_id in healthy
    return roster


def _feature_map(user) -> dict:
    """Feature visibility for the nav. Plain features are on/off; audience-controlled
    features (doctrines, navigation) resolve per-user so the link hides for pilots
    outside the configured audience."""
    from core.features import AUDIENCE_FEATURES, enabled_map, feature_visible_to

    flags = enabled_map()
    for key in AUDIENCE_FEATURES:
        flags[key] = feature_visible_to(key, user)
    return flags


def version(request) -> dict:
    """Expose the deployed source revision to templates (footer build stamp)."""
    from core.version import git_commit

    return {"app_commit": git_commit()}


def csp_nonce(request) -> dict:
    """Expose the per-request CSP nonce so inline <script> tags can authorise
    themselves (set by core.middleware.SecurityHeadersMiddleware). Empty string
    if the middleware did not run (e.g. some error paths) — an inline script with
    an empty nonce is simply blocked, which fails safe."""
    return {"csp_nonce": getattr(request, "csp_nonce", "")}


def _is_registered_alliance_pilot(user, is_member: bool) -> bool:
    """True for a logged-in pilot in an allowed alliance but not the home corp.

    "Allowed" means the home corp's own alliance or any admin-registered partner
    alliance (see ``apps.corporation.access``). Corp members are served by the
    full member nav, so they're excluded here.
    """
    if is_member or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False
    from apps.corporation.access import is_service_alliance_pilot

    return is_service_alliance_pilot(user)
