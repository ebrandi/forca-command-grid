"""User-facing catalog of the opt-in ESI feature scopes.

The scope *strings* live in ``settings.EVE_SSO_FEATURE_SCOPES`` — the single
source of truth the OAuth login flow allowlists. This module attaches the
human-facing metadata (label, what it unlocks, who grants it) so the ESI Scopes
page can present **every** grantable feature, not just a hardcoded few. Without
this, a feature's scope can never be requested through the UI and the feature
stays dead even though the backend supports it.

Keep ``FEATURES`` in sync with ``EVE_SSO_FEATURE_SCOPES``; ``test_sso_scopes``
asserts there are no orphans in either direction.

The label/description/role_hint metadata is display-only (rendered by
``views.scopes_view`` → ``templates/sso/_feature_row.html``) and is built at
import time, so it is marked with ``gettext_lazy``: the proxies resolve per
request, in the viewing pilot's language. The ``key`` and ``audience`` values
are code, never prose — they are compared and round-tripped through
``?feature=`` — and stay untranslated.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils.translation import gettext_lazy as _

# Who can usefully grant a feature. PILOT = any linked character (the data is the
# pilot's own). DIRECTOR = needs an in-game corp role, so it's only offered to
# home-corp members and labelled with the role CCP requires.
PILOT = "pilot"
DIRECTOR = "director"


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    audience: str
    role_hint: str = ""  # in-game role required (shown for director features)

    @property
    def scopes(self) -> list[str]:
        """The ESI scopes this feature requests (from settings — never hardcoded)."""
        return list(settings.EVE_SSO_FEATURE_SCOPES.get(self.key, []))


# Ordered for display: pilot self-service first, then corp/director features.
FEATURES: list[Feature] = [
    Feature(
        "personal_assets", _("Track my assets"),
        _("Show your own assets and where they sit across stations and structures "
          "in the stockpile views."),
        PILOT,
    ),
    Feature(
        "my_industry", _("My industry jobs + blueprints"),
        _("Read your own running industry jobs and owned blueprints, so the Industry "
          "Center can track your personal production and match it to your plans. "
          "While this is granted, corp officers also see your name, manufacturing "
          "slot counts and slot utilisation on the Production Capacity board — the "
          "capacity plan needs named pilots to schedule builds and name bottlenecks. "
          "Revoking this removes your named capacity on the next planning run."),
        PILOT,
    ),
    Feature(
        "freight_search", _("Freight location search"),
        _("Search the player structures you can dock at when booking a courier "
          "contract, so a pickup or drop-off resolves to the exact station."),
        PILOT,
    ),
    Feature(
        "my_contracts", _("Verify my hauls"),
        _("Read your own contracts so the freight service can confirm a courier job "
          "you delivered actually completed in-game and credit you for it."),
        PILOT,
    ),
    Feature(
        "corp_contracts", _("Verify corp hauls"),
        _("Read corp contracts to auto-verify every hauler's courier delivery "
          "in-game before crediting it — no per-pilot grant needed."),
        DIRECTOR, _("Director"),
    ),
    Feature(
        "corp_assets", _("Corp assets"),
        _("Import the corporation's assets to power the stockpile and supply views."),
        DIRECTOR, _("Director"),
    ),
    Feature(
        "corp_roster", _("Member tracking"),
        _("Import the corp roster with each member's location, ship and last login "
          "for the readiness and roster tools."),
        DIRECTOR, _("Director"),
    ),
    Feature(
        "corp_finance", _("Corp wallet"),
        _("Import corp wallet balances and the journal for the finance page."),
        DIRECTOR, _("Accountant or Director"),
    ),
    Feature(
        "corp_contacts", _("Corp standings"),
        _("Import corp contacts to drive the member-facing blue/red standings board."),
        DIRECTOR, _("Director"),
    ),
    Feature(
        "jump_network", _("Jump network"),
        _("List the corp's Ansiblex jump bridges and cyno beacons to auto-build the "
          "jump map and route planner."),
        DIRECTOR, _("Director or Station Manager"),
    ),
    Feature(
        "corp_structures", _("Structure monitoring"),
        _("Monitor every corp structure — fuel remaining, online/low-power state and "
          "reinforcement timers — so nothing runs dry or comes out of reinforcement "
          "unwatched."),
        DIRECTOR, _("Director or Station Manager"),
    ),
    Feature(
        "moon_mining", _("Moon extractions"),
        _("Import scheduled moon extractions for the extraction calendar."),
        DIRECTOR, _("Station Manager or Director"),
    ),
    Feature(
        "corp_industry", _("Corp blueprints + jobs"),
        _("Import the corp's owned blueprints (ME/TE) and running industry jobs so "
          "blueprint coverage and build-or-buy reflect what the corp actually owns "
          "and has in production."),
        DIRECTOR, _("Director or Factory Manager"),
    ),
    Feature(
        "notifications", _("Notification relay"),
        _("Relay in-game notifications — structure attacks, war declarations, "
          "sovereignty and moon pops — to the site and Discord."),
        DIRECTOR, _("Director or role-holder"),
    ),
    Feature(
        "mail_relay", _("Mail relay"),
        _("Relay your corp/alliance mailing-list mail to Discord, so announcements "
          "reach members who don't check in-game mail. Grant from the character "
          "subscribed to the lists."),
        PILOT,
    ),
    Feature(
        "readiness_mail", _("Readiness mail sender"),
        _("Let the platform send readiness alert e-mails in-game from this character. "
          "Grant from the director you select as the readiness mail sender on the "
          "Admin Console → Readiness → Alerts page."),
        DIRECTOR,
    ),
    Feature(
        "pingboard_mail", _("Pingboard mail sender"),
        _("Let Pingboard send corp alerts in-game from this character. Grant from the "
          "director you select as the Pingboard EVE-mail sender on the Admin Console → "
          "Pingboard → Channels page."),
        DIRECTOR,
    ),
    Feature(
        "fleet_tracking", _("Fleet tracking"),
        _("Let an FC pull the live fleet roster to auto-record everyone's "
          "participation (PAP) for an operation — no manual sign-in. Grant from the "
          "character that boss-fleets."),
        PILOT,
    ),
    Feature(
        "mentorship_presence", _("Mentorship session check-in"),
        _("Optional: let the Mentorship Program confirm you were online and in the "
          "session's system during a scheduled mentoring session — the only way to "
          "corroborate 'we flew together', since EVE keeps no history of it. Off by "
          "default, polled only during a session you booked, and never stored beyond "
          "the check."),
        PILOT,
    ),
    Feature(
        "fittings", _("Import saved fits"),
        _("Read your own saved ship fittings so you can import them as corp "
          "doctrines (ESI has no corp-fittings endpoint, so doctrines are seeded "
          "from a director's personal fits)."),
        DIRECTOR, _("Director"),
    ),
    Feature(
        "planetary_industry", _("Import PI colonies"),
        _("Optional: let the Planetary Industry planner import your live colonies so "
          "it can show your real layouts, flag issues (idle extractors, missing routes) "
          "and estimate your current output. EVE only refreshes this when you open the "
          "colony in the client, so imports can be stale — the planner always says when."),
        PILOT,
    ),
]

FEATURES_BY_KEY = {f.key: f for f in FEATURES}


def feature_states(granted_scopes: set[str], audience: str | None = None) -> list[dict]:
    """Annotate each feature with whether all its scopes are already granted.

    Pass ``audience`` to restrict to PILOT or DIRECTOR features; omit for all.
    A feature is "granted" only when *every* scope it needs is present, so a
    partially-granted feature still prompts to complete the grant.
    """
    out: list[dict] = []
    for feature in FEATURES:
        if audience is not None and feature.audience != audience:
            continue
        scopes = feature.scopes
        out.append({
            "feature": feature,
            "granted": bool(scopes) and set(scopes).issubset(granted_scopes),
            "missing": [s for s in scopes if s not in granted_scopes],
        })
    return out
