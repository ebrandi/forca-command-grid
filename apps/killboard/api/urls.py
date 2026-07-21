"""URL routes for the killboard REST API (KB-28), mounted at ``/api/killboard/``.

The OpenAPI schema + docs UI live at ``/api/schema/`` and ``/api/docs/`` (wired in
``config.urls`` so they sit at the ``/api/`` root, not under this namespace).
"""
from __future__ import annotations

from django.urls import path
from rest_framework.routers import SimpleRouter

from .stream import KillboardStreamView
from .views import (
    CorpStatsView,
    HistoryDayView,
    HistoryLatestView,
    KillmailViewSet,
    LeaderboardsView,
    PilotStatsView,
)

app_name = "killboard-api"

router = SimpleRouter(trailing_slash=True)
router.register(r"killmails", KillmailViewSet, basename="killmail")

urlpatterns = [
    path("stats/corp/", CorpStatsView.as_view(), name="stats-corp"),
    path("stats/pilots/<int:character_id>/", PilotStatsView.as_view(), name="stats-pilot"),
    path("leaderboards/", LeaderboardsView.as_view(), name="leaderboards"),
    # ``latest`` must precede the ``<date>`` catch-all so it isn't parsed as a date.
    path("history/latest/", HistoryLatestView.as_view(), name="history-latest"),
    path("history/<str:date>/", HistoryDayView.as_view(), name="history-day"),
    # KB-29 realtime push OUT — bounded SSE, or ?mode=poll for the JSON fallback.
    path("stream/", KillboardStreamView.as_view(), name="stream"),
    *router.urls,
]
