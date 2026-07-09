from __future__ import annotations

from django.urls import path

from . import views

app_name = "navigation"

urlpatterns = [
    path("route/", views.route_planner, name="route_planner"),
    path("jump/", views.jump_planner, name="jump_planner"),
    path("jump/routes/", views.jump_routes, name="jump_routes"),
    path("jump/routes/save/", views.jump_route_save, name="jump_route_save"),
    path("jump/routes/<int:pk>/open/", views.jump_route_open, name="jump_route_open"),
    path("jump/routes/<int:pk>/delete/", views.jump_route_delete, name="jump_route_delete"),
    path("jump/routes/<int:pk>/watch/", views.jump_route_watch, name="jump_route_watch"),
    path("range/", views.range_finder, name="range_finder"),
    path("map/", views.map_index, name="map_index"),
    path("map/<str:region>/", views.map_region, name="map_region"),
    path("route-map/", views.route_map_view, name="route_map"),
    path("range-map/", views.range_map_view, name="range_map"),
    path("system/<str:system>/", views.system_detail, name="system_detail"),
    path("roaming/", views.roaming, name="roaming"),
    path("gatecamp/", views.gatecamp, name="gatecamp"),
    path("beacons/", views.beacons, name="beacons"),
    path("beacons/add/", views.beacon_add, name="beacon_add"),
    path("beacons/sync/", views.beacon_sync, name="beacon_sync"),
    path("beacons/<int:pk>/remove/", views.beacon_remove, name="beacon_remove"),
    path("systems/", views.system_search, name="system_search"),
]
