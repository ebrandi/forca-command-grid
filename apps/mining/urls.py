from __future__ import annotations

from django.urls import path

from . import views

app_name = "mining"

urlpatterns = [
    path("", views.ledger, name="ledger"),
    path("me/", views.my_mining, name="me"),
    path("sync/", views.sync_ledger, name="sync"),
    path("tax/", views.set_tax, name="set_tax"),
    path("payouts/", views.payouts, name="payouts"),
    path("payouts/create/", views.payout_create, name="payout_create"),
    path("payouts/<int:pk>/", views.payout_detail, name="payout"),
    path("payouts/<int:pk>/recompute/", views.payout_recompute, name="payout_recompute"),
    path("payouts/<int:pk>/finalise/", views.payout_finalise, name="payout_finalise"),
    path("payouts/<int:pk>/lines/<int:line_id>/paid/", views.line_paid, name="line_paid"),
]
