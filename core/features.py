"""Leader-configurable feature flags — which member-facing features pilots see.

**Default is everything enabled.** A feature is on unless an officer explicitly
turns it off on the Admin Console → Features page. The disabled set is stored in a
single ``AppSetting`` row and cached per process (invalidated on save), so the
per-request nav check is a cache read, not a query.

Member services (Freight, Buyback, Corp Store) are *not* listed here: they already
have their own richer audience config (disabled / corp / alliance / public) on
their own settings pages. The Features page links to those for one-stop discovery.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps

from django.http import Http404

_SETTING_KEY = "features.disabled"
_CACHE_KEY = "features:disabled:v2"
_CACHE_TTL = 600


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    group: str


# The toggleable member-facing features, grouped for the admin page. Keys are
# stable identifiers used by the nav (``features.<key>``) and the
# ``feature_required`` view guard — do not rename without a data migration.
#
# ORDER MATTERS: the Services & features page renders these with
# ``itertools.groupby(FEATURES, key=f.group)`` and does NOT pre-sort, so every
# feature sharing a ``group`` MUST stay contiguous here or the page grows a
# duplicate section header. Keep new features next to their group-mates.
FEATURES: list[Feature] = [
    Feature("killboard", "Killboard", "Kills, losses, rankings and combat stats.", "Community & intel"),
    Feature("hall_of_fame", "Hall of Fame", "Monthly corp recognition leaderboard.", "Community & intel"),
    Feature("knowledge_base", "Knowledge base", "Corp wiki / knowledge pages.", "Community & intel"),
    Feature("onboarding", "New-player onboarding", "The new-pilot checklist and join flow.", "Community & intel"),
    Feature("mentorship", "Mentorship Program",
            "The cadet/veteran mentorship programme: registration, pairing, learning tracks, "
            "field exercises, recognition and the leadership dashboard.", "Community & intel"),
    Feature("raffle", "Raffle contests",
            "Engagement raffles: pilots earn tickets from PVP and other activity, leaders run "
            "fair commit-reveal draws, and the dashboard drives ESI enrolment.", "Community & intel"),
    Feature("doctrines", "Doctrines & Shipyard", "Doctrine library, readiness and ship ordering.", "Ships & doctrines"),
    Feature("operations", "Fleet operations", "Operations planner, timers, RSVP and PAP.", "Fleet & combat"),
    Feature("intel", "Intel tools", "Watchlists, roaming targets and gate-camp watch.", "Fleet & combat"),
    Feature("standings", "Standings board", "The blue/red contacts standings board.", "Fleet & combat"),
    Feature("structures", "Structures", "Corp structure fuel, state and reinforcement timers.", "Fleet & combat"),
    Feature("navigation", "Navigation & maps", "Route/jump planners, region maps and jump bridges.", "Navigation"),
    Feature("industry", "Industry & production", "BOM, build jobs, supply and moon extractions.", "Industry & economy"),
    Feature("planetary", "Planetary Industry",
            "The PI guide & planner: planets, production chains, profit and colony import.",
            "Industry & economy"),
    Feature("mining", "Mining", "Mining ledger and payouts.", "Industry & economy"),
    Feature("market", "Market", "Market dashboard and trade signals.", "Industry & economy"),
    Feature("stockpile", "Stockpile & assets", "Corp stockpile, assets and corp logistics.", "Industry & economy"),
    Feature("corp_contracts", "Corp contracts",
            "The officer corp-contract browser (item exchange, courier, auction).", "Industry & economy"),
    Feature("finance", "Corp finance",
            "Wallet balances, income/expense, forecast and journal (needs the corp wallet ESI scope).",
            "Industry & economy"),
    Feature("srp", "Ship replacement (SRP)", "The ship-replacement programme and claims.", "Pilot tools"),
    Feature("skills", "Skill plans", "Skill planning toward doctrines.", "Pilot tools"),
    Feature("tasks", "Tasks", "The corp task board.", "Pilot tools"),
    Feature("briefing", "Daily briefing",
            "The Command Center's time-sensitive digest, plus the officer command deck.",
            "Pilot tools"),
    Feature("contributions", "Contribution ledger",
            "The member 'My Contribution' activity ledger and recognition feed.", "Pilot tools"),
    Feature("command_intel_pilot", "Pilot Intelligence",
            "Corp orders in the Command Center quest log — each member's ranked moves toward "
            "the corp's binding constraints.",
            "Pilot tools"),
    Feature("command_intel", "Command Intelligence",
            "Officer strategic intelligence: reports, constraints, COAs, campaigns, the what-if "
            "simulator, Ask and battle after-action reviews.", "Command & readiness"),
    Feature("readiness", "Readiness platform",
            "Readiness scoring: the officer dashboard, risk register, alerts, weekly report, timeline, "
            "fleet simulator, and each pilot's readiness panel + quests on the Command Center.",
            "Command & readiness"),
    Feature("recommendations", "Recommendations & alerts",
            "The officer command board, the Command Center's pick-up-work boards, and the "
            "in-game / Discord alert relay.",
            "Command & readiness"),
    Feature("pingboard", "Pingboard",
            "The unified alerting + calendar system: the dashboard, officer alert composer "
            "and history, and the corporation calendar of operations, timers and reminders.",
            "Command & readiness"),
    Feature("recruitment", "Recruitment",
            "The recruiter's candidate tracker and evidence desk.", "Leadership"),
]

FEATURES_BY_KEY = {f.key: f for f in FEATURES}

# --- Audience-controlled features -------------------------------------------
# A few features are not a plain on/off but choose *who* can see them — the same
# 4-state audience the member services use: disabled / corp / corp+alliance / public.
# "alliance" includes registered partner alliances AND friendly corporations (via
# apps.corporation.access.is_service_alliance_pilot). Defaults preserve today's
# behaviour: the doctrine library was corp-only; the navigation tools were public.
AUDIENCE_DISABLED = "disabled"
AUDIENCE_CORP = "corp"
AUDIENCE_ALLIANCE = "alliance"
AUDIENCE_PUBLIC = "public"
AUDIENCE_VALUES = (AUDIENCE_DISABLED, AUDIENCE_CORP, AUDIENCE_ALLIANCE, AUDIENCE_PUBLIC)

AUDIENCE_FEATURES: dict[str, str] = {
    "doctrines": AUDIENCE_CORP,
    "navigation": AUDIENCE_PUBLIC,
    # Raffles default to corp-only; leadership can open them to alliance/friendly
    # corps or the public. Shipped enabled (default-everything-on) — but no pilot
    # sees anything until leadership creates AND activates a contest.
    "raffle": AUDIENCE_CORP,
}
_AUDIENCE_SETTING_KEY = "features.audience"
_AUDIENCE_CACHE_KEY = "features:audience:v1"


def feature_audiences() -> dict[str, str]:
    """``{key: audience}`` for every audience-feature (cached; defaults merged in)."""
    from django.core.cache import cache

    cached = cache.get(_AUDIENCE_CACHE_KEY)
    if cached is None:
        from apps.admin_audit.models import AppSetting

        stored = (AppSetting.get(_AUDIENCE_SETTING_KEY, {}) or {}).get("audience", {})
        cached = {}
        for key, default in AUDIENCE_FEATURES.items():
            value = stored.get(key)
            cached[key] = value if value in AUDIENCE_VALUES else default
        cache.set(_AUDIENCE_CACHE_KEY, cached, _CACHE_TTL)
    return dict(cached)


def feature_audience(key: str) -> str:
    """The configured audience for an audience-feature (its default if unset/invalid)."""
    return feature_audiences().get(key, AUDIENCE_FEATURES.get(key, AUDIENCE_CORP))


def set_feature_audiences(mapping, *, user=None) -> dict[str, str]:
    """Persist audience choices for the audience-features (merge, ignore junk) + bust cache."""
    from django.core.cache import cache

    from apps.admin_audit.models import AppSetting

    clean = dict(feature_audiences())
    for key in AUDIENCE_FEATURES:
        chosen = mapping.get(key)
        if chosen in AUDIENCE_VALUES:
            clean[key] = chosen
    AppSetting.objects.update_or_create(
        key=_AUDIENCE_SETTING_KEY,
        defaults={"value": {"audience": clean}, "updated_by": user},
    )
    cache.delete(_AUDIENCE_CACHE_KEY)
    return clean


def feature_visible_to(key: str, user) -> bool:
    """Whether ``user`` may see/use a feature under its configured audience.

    Mirrors the member-service ``can_access``: public → everyone; disabled → nobody;
    corp → corp members; alliance → members plus registered alliance / friendly-corp
    pilots. A non-audience feature falls back to its plain enabled state.
    """
    if key not in AUDIENCE_FEATURES:
        return feature_enabled(key)
    aud = feature_audience(key)
    if aud == AUDIENCE_DISABLED:
        return False
    if aud == AUDIENCE_PUBLIC:
        return True
    from core import rbac

    if not getattr(user, "is_authenticated", False):
        return False
    if rbac.has_role(user, rbac.ROLE_MEMBER):
        return True
    if aud == AUDIENCE_ALLIANCE:
        from apps.corporation.access import is_service_alliance_pilot

        return is_service_alliance_pilot(user)
    return False


def disabled_set() -> set[str]:
    """The set of explicitly-disabled feature keys (cached)."""
    from django.core.cache import cache

    cached = cache.get(_CACHE_KEY)
    if cached is None:
        from apps.admin_audit.models import AppSetting

        value = AppSetting.get(_SETTING_KEY, {}) or {}
        cached = {k for k in value.get("disabled", []) if k in FEATURES_BY_KEY}
        cache.set(_CACHE_KEY, cached, _CACHE_TTL)
    return cached


def feature_enabled(key: str) -> bool:
    """True unless the feature was explicitly turned off (default everything on).

    For audience-controlled features, "enabled" means the audience isn't ``disabled``
    (who can see it is a separate, per-user question — see ``feature_visible_to``).
    """
    if key in AUDIENCE_FEATURES:
        return feature_audience(key) != AUDIENCE_DISABLED
    return key not in disabled_set()


def enabled_map() -> dict[str, bool]:
    """``{feature_key: enabled}`` for every known feature — for templates."""
    disabled = disabled_set()
    return {f.key: (f.key not in disabled) for f in FEATURES}


def set_disabled(keys, *, user=None) -> set[str]:
    """Persist the disabled set (ignoring unknown keys) and bust the cache."""
    from django.core.cache import cache

    from apps.admin_audit.models import AppSetting

    valid = sorted({k for k in keys if k in FEATURES_BY_KEY})
    AppSetting.objects.update_or_create(
        key=_SETTING_KEY,
        defaults={"value": {"disabled": valid}, "updated_by": user},
    )
    cache.delete(_CACHE_KEY)
    return set(valid)


def feature_required(key: str):
    """View decorator: 404 a feature's pages when leadership has it turned off."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not feature_enabled(key):
                raise Http404("This feature is not enabled for this corporation.")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


# --- URL → feature mapping, for the gate middleware --------------------------
# A whole namespace maps to one feature (the common case). Namespaces NOT listed
# here are never gated (leadership/account/core pages, plus the member services
# which have their own audience config).
_NAMESPACE_FEATURE = {
    "market": "market",
    "mining": "mining",
    "industry": "industry",
    "planetary": "planetary",
    "erp": "industry",
    "skills": "skills",
    "tasks": "tasks",
    "kb": "knowledge_base",
    "onboarding": "onboarding",
    "mentorship": "mentorship",
    "doctrines": "doctrines",
    "operations": "operations",
    "srp": "srp",
    "stockpile": "stockpile",
    "navigation": "navigation",
    "killboard": "killboard",
    "raffle": "raffle",
    # Whole strategic/pipeline namespaces. readiness + recommendations each span
    # member and officer views but have no public routes, so one map gates the lot.
    "readiness": "readiness",
    "recommendations": "recommendations",
    # command_intel's officer surface; the member '/command/me/' pilot slice is
    # kept on command_intel_pilot by the _VIEW_FEATURE overrides below.
    "command_intel": "command_intel",
    # recruitment: this also gates the candidate OAuth begin/callback, which is the
    # intended behaviour — when recruitment is off, no candidate should be able to link.
    "recruitment": "recruitment",
}

# Per-view overrides where one namespace spans two features (intel lives across
# the killboard and navigation apps), or where only specific views in an
# otherwise-ungated namespace belong to a feature.
_VIEW_FEATURE = {
    ("killboard", "watchlists"): "intel",
    ("killboard", "watchlist_detail"): "intel",
    ("killboard", "watchlist_create"): "intel",
    ("killboard", "watchlist_add_entry"): "intel",
    ("killboard", "watchlist_remove_entry"): "intel",
    ("killboard", "watchlist_delete"): "intel",
    ("killboard", "battle_report_detail"): "intel",
    ("killboard", "battle_report_create"): "intel",
    ("navigation", "roaming"): "intel",
    ("navigation", "gatecamp"): "intel",
    ("corporation", "standings"): "standings",
    ("corporation", "standings_sync"): "standings",
    ("corporation", "extractions"): "industry",
    ("corporation", "extractions_sync"): "industry",
    ("pilots", "hall_of_fame"): "hall_of_fame",
    # command_intel is namespace-mapped to the officer 'command_intel' feature;
    # keep the two member pilot views on their own 'command_intel_pilot' key so
    # the officer surface and the pilot quest-log toggle independently.
    ("command_intel", "me"): "command_intel_pilot",
    ("command_intel", "directive_action"): "command_intel_pilot",
    # Per-view gates in namespaces that must stay partly ungated: the corporation
    # namespace keeps roster/compliance reachable, and logistics is the
    # audience-gated freight service (not a Feature), so gate only these views.
    ("corporation", "structures"): "structures",
    ("corporation", "structures_sync"): "structures",
    ("corporation", "finance"): "finance",
    ("corporation", "finance_sync"): "finance",
    ("corporation", "income"): "finance",
    ("logistics", "corp_contracts"): "corp_contracts",
    # pilots namespace: hall_of_fame/contributions map to their own features; the
    # ledger's recognition toggle rides with the contributions key. The merged
    # Daily Briefing (pilots:briefing) is NOT middleware-gated — it spans three
    # feature keys (briefing / command_intel_pilot / recommendations) and gates
    # itself in-view: 404 only when all three are off.
    ("pilots", "contributions"): "contributions",
    ("pilots", "toggle_recognition"): "contributions",
}


def feature_for_view(namespace: str | None, url_name: str | None) -> str | None:
    """The feature a resolved view belongs to, or None if it is never gated."""
    if (namespace, url_name) in _VIEW_FEATURE:
        return _VIEW_FEATURE[(namespace, url_name)]
    return _NAMESPACE_FEATURE.get(namespace)


class FeatureGateMiddleware:
    """404 any view whose feature leadership has turned off (defence in depth).

    The nav already hides disabled features; this stops a direct URL from reaching
    the view. Keyed off the resolved namespace/url_name so it's one place to audit.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        if match is None:
            return None
        feature = feature_for_view(match.namespace, match.url_name)
        if not feature:
            return None
        # Audience-controlled features 404 for anyone outside their audience (not just
        # when fully disabled); plain features 404 only when turned off.
        if feature in AUDIENCE_FEATURES:
            if not feature_visible_to(feature, getattr(request, "user", None)):
                raise Http404("This feature is not available.")
        elif not feature_enabled(feature):
            raise Http404("This feature is not enabled for this corporation.")
        return None
