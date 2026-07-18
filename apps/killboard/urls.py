from __future__ import annotations

from django.urls import path

from . import views

app_name = "killboard"

urlpatterns = [
    path("", views.killboard_list, name="list"),
    path("rankings/", views.killboard_rankings, name="rankings"),
    path("stats/", views.killboard_stats, name="stats"),
    path("roster/", views.killboard_roster, name="roster"),
    path("pilot/<int:character_id>/", views.killboard_pilot, name="pilot"),
    path("compare/", views.killboard_compare, name="compare"),
    # Intel — keep these above the <int:killmail_id> catch-all.
    path("intel/", views.watchlists, name="watchlists"),
    path("intel/systems/", views.system_search, name="system_search"),
    path("intel/create/", views.watchlist_create, name="watchlist_create"),
    path("intel/<int:pk>/", views.watchlist_detail, name="watchlist_detail"),
    path("intel/<int:pk>/add/", views.watchlist_add_entry, name="watchlist_add_entry"),
    path("intel/<int:pk>/entries/<int:entry_id>/remove/", views.watchlist_remove_entry, name="watchlist_remove_entry"),
    path("intel/<int:pk>/delete/", views.watchlist_delete, name="watchlist_delete"),
    path("battles/create/", views.battle_report_create, name="battle_report_create"),
    path("battles/<int:pk>/", views.battle_report_detail, name="battle_report_detail"),
    path("killfeed/settings/", views.killfeed_config, name="killfeed_config"),
    # Fit exports — keep above the <int:killmail_id> single-segment catch-all.
    path("<int:killmail_id>/eft/", views.killmail_eft, name="eft"),
    path("<int:killmail_id>/fit.json", views.killmail_fit_esi, name="fit_esi"),
    path("<int:killmail_id>/", views.killmail_detail, name="detail"),
]
