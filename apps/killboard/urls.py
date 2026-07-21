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
    # KB-31 public permalink — static "battles/r/" prefix, above the <int:pk> route.
    path("battles/r/<str:slug>/", views.battle_report_public, name="battle_report_public"),
    path("battles/<int:pk>/", views.battle_report_detail, name="battle_report_detail"),
    path("battles/<int:pk>/recompute/", views.battle_report_recompute, name="battle_report_recompute"),
    path("battles/<int:pk>/side/move/", views.battle_report_side_move, name="battle_report_side_move"),
    path("killfeed/settings/", views.killfeed_config, name="killfeed_config"),
    # KB-29 live feed: server-rendered single row the live JS prepends. Above the
    # <int:killmail_id> single-segment catch-all.
    path("live/row/<int:killmail_id>/", views.killmail_feed_row, name="live_row"),
    # KB-28 self-serve API tokens — above the <int:killmail_id> single-segment catch-all.
    path("api-tokens/", views.api_tokens, name="api_tokens"),
    path("api-tokens/create/", views.api_token_create, name="api_token_create"),
    path("api-tokens/<int:token_id>/revoke/", views.api_token_revoke, name="api_token_revoke"),
    # KB-30 self-serve per-pilot subscriptions — above the <int:killmail_id> catch-all.
    path("subscriptions/", views.subscriptions, name="subscriptions"),
    path("subscriptions/create/", views.subscription_create, name="subscription_create"),
    path("subscriptions/<int:sub_id>/toggle/", views.subscription_toggle, name="subscription_toggle"),
    path("subscriptions/<int:sub_id>/delete/", views.subscription_delete, name="subscription_delete"),
    path("subscriptions/<int:sub_id>/test/", views.subscription_test, name="subscription_test"),
    path("subscriptions/<int:sub_id>/rss/regenerate/", views.subscription_rss_regenerate, name="subscription_rss_regenerate"),
    # The token-authed RSS feed (no session). Static prefix, so it never shadows the catch-all.
    path("subscriptions/feed/<str:rss_token>/", views.subscription_feed, name="subscription_feed"),
    # Fit exports + comments — keep above the <int:killmail_id> single-segment catch-all.
    path("<int:killmail_id>/eft/", views.killmail_eft, name="eft"),
    path("<int:killmail_id>/fit.json", views.killmail_fit_esi, name="fit_esi"),
    path("<int:killmail_id>/comment/", views.killmail_comment_create, name="comment_create"),
    path("<int:killmail_id>/comment/<int:comment_id>/delete/", views.killmail_comment_delete, name="comment_delete"),
    path("<int:killmail_id>/", views.killmail_detail, name="detail"),
]
