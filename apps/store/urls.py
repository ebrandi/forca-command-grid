from __future__ import annotations

from django.urls import path

from . import views

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
    path("settings/", views.config, name="config"),
]
