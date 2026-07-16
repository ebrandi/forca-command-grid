from __future__ import annotations

from django.urls import path

from . import views, views_inventory

app_name = "store"

urlpatterns = [
    path("", views.storefront, name="storefront"),
    path("hulls/search/", views.hull_search, name="hull_search"),
    path("systems/search/", views.system_search, name="system_search"),
    path("order/fit/", views.order_fit, name="order_fit"),
    path("order/hull/", views.order_hull, name="order_hull"),
    path("orders/mine/", views.my_orders, name="my_orders"),
    path("orders/<int:pk>/", views.order_detail, name="order"),
    path("board/", views.board, name="board"),
    path("supply/forecast/", views.supply_forecast, name="supply_forecast"),
    path("orders/<int:pk>/claim/", views.claim_order, name="claim_order"),
    path("orders/<int:pk>/advance/", views.advance_order, name="advance_order"),
    path("orders/<int:pk>/action/", views.order_action, name="order_action"),
    path("orders/<int:pk>/eta/", views.order_eta, name="order_eta"),
    path("waitlist/<int:fit_id>/", views.waitlist_toggle, name="waitlist_toggle"),
    path("settings/", views.config, name="config"),
    # Officer inventory console (SHIP-1)
    path("inventory/", views_inventory.inventory, name="inventory"),
    path("inventory/policy/", views_inventory.shipyard_policy, name="shipyard_policy"),
    path("inventory/bulk/", views_inventory.inventory_bulk, name="inventory_bulk"),
    path("inventory/fit/<int:fit_id>/", views_inventory.inventory_fit, name="inventory_fit"),
    path("inventory/fit/<int:fit_id>/receipt/", views_inventory.inventory_receipt,
         name="inventory_receipt"),
    path("inventory/stock/<int:stock_id>/adjust/", views_inventory.inventory_adjust,
         name="inventory_adjust"),
    path("inventory/stock/<int:stock_id>/revalidate/", views_inventory.inventory_revalidate,
         name="inventory_revalidate"),
    path("inventory/need/<int:need_id>/action/", views_inventory.supply_action,
         name="supply_action"),
]
