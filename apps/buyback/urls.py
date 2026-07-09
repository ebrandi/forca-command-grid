from __future__ import annotations

from django.urls import path

from . import views

app_name = "buyback"

urlpatterns = [
    path("", views.appraisal, name="appraisal"),
    path("submit/", views.submit_offer, name="submit_offer"),
    path("board/", views.board, name="board"),
    path("offers/<int:pk>/", views.offer_detail, name="offer"),
    path("offers/<int:pk>/buy/", views.buy_offer, name="buy_offer"),
    path("offers/<int:pk>/action/", views.offer_action, name="offer_action"),
    path("settings/", views.config, name="config"),
    # Corp-funded guaranteed buyback (4.20)
    path("guaranteed/request/", views.guaranteed_request, name="guaranteed_request"),
    path("guaranteed/<int:pk>/cancel/", views.guaranteed_cancel, name="guaranteed_cancel"),
    path("guaranteed/queue/", views.guaranteed_queue, name="guaranteed_queue"),
    path("guaranteed/<int:pk>/decide/", views.guaranteed_decide, name="guaranteed_decide"),
    path("guaranteed/<int:pk>/settle/", views.guaranteed_settle, name="guaranteed_settle"),
]
