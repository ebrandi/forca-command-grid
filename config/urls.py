"""Root URL configuration."""
from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.killboard import signature_public
from apps.killboard.api import views as killboard_api_views

from . import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("features/", views.showcase, name="showcase"),
    path("healthz", views.healthz, name="healthz"),
    # Language selector POST target (set_language) + the JS message catalogue (jsi18n).
    path("i18n/", include("core.i18n.urls")),
    path("auth/eve/", include("apps.sso.urls")),
    path("", include("apps.identity.urls")),
    path("killboard/", include("apps.killboard.urls")),
    # Combat Signatures — the PUBLIC banner PNG (WS-5). Root-level and outside the killboard
    # namespace (like /healthz): pilots embed a stable /s/<token>.png in forums and Discord.
    # nginx serves the rendered file straight from disk in prod; this Django route is the
    # dev/test path and the prod fallback for pending / disabled / unknown tokens.
    path("s/<str:token>.png", signature_public.signature_png, name="signature_public"),
    path("doctrines/", include("apps.doctrines.urls")),
    path("lab/", include("apps.fitting.urls")),  # Tocha's Lab (ship fitting)
    path("industry/pi/", include("apps.planetary.urls")),
    path("industry/", include("apps.industry.urls")),
    path("skills/", include("apps.skills.urls")),
    path("stockpile/", include("apps.stockpile.urls")),
    path("market/", include("apps.market.urls")),
    path("onboarding/", include("apps.onboarding.urls")),
    path("mentorship/", include("apps.mentorship.urls")),
    path("recommendations/", include("apps.recommendations.urls")),
    path("pingboard/", include("apps.pingboard.urls")),
    path("ops/", include("apps.admin_audit.urls")),
    path("impersonation/", include("apps.impersonation.urls")),
    path("pilots/", include("apps.pilots.urls")),
    path("tasks/", include("apps.tasks.urls")),
    path("srp/", include("apps.srp.urls")),
    path("raffle/", include("apps.raffle.urls")),
    path("readiness/", include("apps.readiness.urls")),
    path("operations/", include("apps.operations.urls")),
    path("mining/", include("apps.mining.urls")),
    path("erp/", include("apps.erp.urls")),
    path("kb/", include("apps.kb.urls")),
    path("recruitment/", include("apps.recruitment.urls")),
    path("roster/", include("apps.corporation.urls")),
    path("freight/", include("apps.logistics.urls")),
    path("buyback/", include("apps.buyback.urls")),
    path("store/", include("apps.store.urls")),
    path("procurement/", include("apps.procurement.urls")),
    path("supply-board/", include("apps.supplyboard.urls")),
    path("tools/", include("apps.navigation.urls")),
    path("command/", include("apps.command_intel.urls")),
    path("campaigns/", include("apps.campaigns.urls")),
    path("capsuleer/", include("apps.capsuleer.urls")),
    path("comms/", include("apps.comms_access.urls")),
    # KB-28 — the killboard REST API + its OpenAPI schema/docs. The schema + Swagger UI
    # sit at the /api/ root (per the KB-28 taxonomy); the resource endpoints live under
    # /api/killboard/. Both are RBAC-gated in their views (member+, or the public-read
    # subset when KILLBOARD_API_PUBLIC_READ is on).
    path("api/killboard/", include("apps.killboard.api.urls")),
    path("api/schema/", killboard_api_views.KillboardSchemaView.as_view(), name="killboard-api-schema"),
    path(
        "api/docs/",
        killboard_api_views.KillboardDocsView.as_view(url_name="killboard-api-schema"),
        name="killboard-api-docs",
    ),
]

# The stock Django admin is superseded by the native /ops/ console (OFFICER/Director
# gated). It is disabled by default in production (see settings/prod.py) to remove the
# guessable /admin/ login form and the full model-admin surface from the attack surface;
# EVE-SSO accounts never receive is_staff, so nothing can log in there anyway. Opt back
# in with DJANGO_ENABLE_ADMIN=1 if a break-glass superuser workflow is ever needed.
if getattr(settings, "ENABLE_DJANGO_ADMIN", True):
    urlpatterns.append(path("admin/", admin.site.urls))
