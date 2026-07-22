"""KB-38 — the self-host setup wizard's live status model (WS-D5).

Not a state machine: every step recomputes its status from ground truth on each page load,
so the wizard can never drift out of sync with reality (a token revoked at CCP, an SDE
re-import, a deleted price table all reflect immediately). Each step returns a status chip
(``ok`` / ``warn`` / ``missing``) plus the exact remedial action.

The steps are intentionally cheap — no ESI calls on a GET. The Director-token step pairs a
DB-only "a candidate token exists" check with the authoritative "did the corp feed actually
poll?" health signal, rather than re-running the per-candidate ESI Director verification that
the ingest task uses (see ``tasks.corp_killmail_feed_token_present``).
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

OK = "ok"
WARN = "warn"
MISSING = "missing"

# Below this many SdeType rows the fitting/killboard name + slot data is clearly not imported.
_SDE_MIN_TYPES = 1000
# The corp ESI feed polls every ~15 min; no success in this long means the Director token or
# the beat scheduler is not working, even if a candidate token is present.
_CORP_FEED_STALE = timedelta(hours=2)


def _expected_callback(request) -> str:
    """The callback URL a corp must register on developers.eveonline.com, from this host."""
    if request is None:
        return getattr(settings, "EVE_SSO_CALLBACK_URL", "")
    return request.build_absolute_uri("/auth/eve/callback/")


def step_esi_app(request) -> dict:
    """Step 1 — is the EVE SSO application configured (client id + secret)?"""
    configured = bool(
        getattr(settings, "EVE_SSO_CLIENT_ID", "")
        and getattr(settings, "EVE_SSO_CLIENT_SECRET", "")
    )
    return {
        "id": "esi_app",
        "title": _("EVE SSO application"),
        "status": OK if configured else MISSING,
        "detail": (
            _("The EVE SSO application is configured — pilots can log in and grant ESI scopes.")
            if configured else
            _("No EVE SSO application is configured. Register one on developers.eveonline.com "
              "and set EVE_SSO_CLIENT_ID and EVE_SSO_CLIENT_SECRET in your environment.")
        ),
        "callback_url": _expected_callback(request),
        "configured_callback": getattr(settings, "EVE_SSO_CALLBACK_URL", ""),
    }


def step_director_token(corp_id: int) -> dict:
    """Step 2 — a corp-killmails Director token whose feed is actually polling."""
    from .models import IngestSourceHealth
    from .tasks import corp_killmail_feed_token_present

    has_token = bool(corp_id) and corp_killmail_feed_token_present(corp_id)
    health = IngestSourceHealth.objects.filter(source="esi_corp").first()
    last_success = health.last_success_at if health else None
    fresh = last_success is not None and (timezone.now() - last_success) <= _CORP_FEED_STALE

    if fresh:
        status = OK
        detail = _("The corporation killmail feed is polling successfully — a Director token "
                   "is granted and working.")
    elif has_token:
        status = WARN
        detail = _("A corp-killmails token exists, but the corporation feed has not polled "
                   "recently. Confirm the token's character still holds the in-game Director "
                   "role, and that the background scheduler is running.")
    else:
        status = MISSING
        detail = _("No corp-killmails Director token is present. A director must log in and "
                   "grant the corporation killmail scope for their character.")
    return {
        "id": "director_token",
        "title": _("Director killmail token"),
        "status": status,
        "detail": detail,
        "has_token": has_token,
        "last_success_at": last_success,
        "consecutive_failures": health.consecutive_failures if health else 0,
        "last_error": health.last_error if health else "",
    }


def step_reference_data() -> dict:
    """Step 3 — SDE type data + market prices present (killboard rendering + valuation)."""
    from apps.market.models import MarketPrice
    from apps.sde.models import SdeType

    type_count = SdeType.objects.count()
    price_count = MarketPrice.objects.count()
    has_sde = type_count >= _SDE_MIN_TYPES
    has_prices = price_count > 0

    if has_sde and has_prices:
        status, detail = OK, _("Ship/item names and market prices are loaded — kills render "
                               "with fits and ISK values.")
    elif has_sde:
        status = WARN
        detail = _("Ship/item data is loaded, but no market prices are present, so kills "
                   "will show without ISK values. Run the market price import.")
    else:
        status = MISSING
        detail = _("The static data export (SDE) is not loaded. Run the SDE import so kills "
                   "can resolve ship and item names.")
    return {
        "id": "reference_data",
        "title": _("Ship data & prices"),
        "status": status,
        "detail": detail,
        "type_count": type_count,
        "price_count": price_count,
    }


def step_history(active_import) -> dict:
    """Step 4 — is the board populated? (plus the one-click import launcher state)."""
    from .models import Killmail

    agg = Killmail.objects.count()
    oldest = newest = None
    if agg:
        span = Killmail.objects.order_by("killmail_time").values_list("killmail_time", flat=True)
        oldest = span.first()
        newest = Killmail.objects.order_by("-killmail_time").values_list(
            "killmail_time", flat=True).first()

    if agg:
        status, detail = OK, _("The board holds %(n)s killmails.") % {"n": f"{agg:,}"}
    else:
        status = WARN
        detail = _("The board is empty. Import your corporation's history so members see a "
                   "populated killboard from minute one.")
    return {
        "id": "history",
        "title": _("Killmail history"),
        "status": status,
        "detail": detail,
        "killmail_count": agg,
        "oldest": oldest,
        "newest": newest,
        "active_import": active_import,
    }


def step_branding() -> dict:
    """Step 5 — optional corp branding (never blocks; a bare install is fine)."""
    from . import branding

    b = branding.get_branding()
    configured = any(b.values())
    return {
        "id": "branding",
        "title": _("Branding"),
        # Branding is optional, so "not set" is a soft warn (a nudge), never missing/blocking.
        "status": OK if configured else WARN,
        "detail": (
            _("Your corporation's name, logo and accent colour are set.") if configured else
            _("Optional: give the board your corporation's name, logo and accent colour so it "
              "feels like yours.")
        ),
        "branding": b,
    }


def wizard_steps(request, corp_id: int, active_import=None) -> list[dict]:
    """All five setup steps, computed live, in wizard order."""
    return [
        step_esi_app(request),
        step_director_token(corp_id),
        step_reference_data(),
        step_history(active_import),
        step_branding(),
    ]
