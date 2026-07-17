"""Root URL configuration."""
from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

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
    path("doctrines/", include("apps.doctrines.urls")),
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
]

# The stock Django admin is superseded by the native /ops/ console (OFFICER/Director
# gated). It is disabled by default in production (see settings/prod.py) to remove the
# guessable /admin/ login form and the full model-admin surface from the attack surface;
# EVE-SSO accounts never receive is_staff, so nothing can log in there anyway. Opt back
# in with DJANGO_ENABLE_ADMIN=1 if a break-glass superuser workflow is ever needed.
if getattr(settings, "ENABLE_DJANGO_ADMIN", True):
    urlpatterns.append(path("admin/", admin.site.urls))
