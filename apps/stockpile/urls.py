from __future__ import annotations

from django.urls import path

from . import views

app_name = "stockpile"

urlpatterns = [
    path("", views.stockpile_dashboard, name="dashboard"),
    path("assets/", views.assets_view, name="assets"),
    path("assets/items/", views.asset_location_items, name="asset_items"),
    path("assets/search/", views.asset_search, name="asset_search"),
    path("assets/sync-corp/", views.sync_corp_assets, name="sync_corp_assets"),
    path("assets/sync-mine/", views.sync_my_assets, name="sync_my_assets"),
    path("stock/record/", views.record_stock, name="record_stock"),
    path("stockpiles/create/", views.create_stockpile, name="create_stockpile"),
    path("logistics/", views.logistics_board, name="logistics"),
    path("logistics/post/", views.create_haul, name="create_haul"),
    path("logistics/<int:pk>/claim/", views.claim_haul, name="claim_haul"),
    path("logistics/<int:pk>/transition/", views.haul_transition, name="haul_transition"),
]
