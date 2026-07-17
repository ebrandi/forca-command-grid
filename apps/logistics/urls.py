from __future__ import annotations

from django.urls import path

from . import views, views_freight

app_name = "logistics"

urlpatterns = [
    path("", views.calculator, name="calculator"),
    path("systems/", views.system_search, name="system_search"),
    path("locations/", views.location_search, name="location_search"),
    path("contracts/", views.contracts, name="contracts"),
    path("contracts/post/", views.post_contract, name="post_contract"),
    path("contracts/<int:pk>/claim/", views.claim_contract, name="claim_contract"),
    path("contracts/<int:pk>/transition/", views.transition_contract, name="transition_contract"),
    path("contracts/<int:pk>/cancel/", views.cancel_contract, name="cancel_contract"),
    path("rates/", views.rates, name="rates"),
    path("corp-contracts/", views.corp_contracts, name="corp_contracts"),
    # Freight pipeline (P6) — officer-only. Mounted under /freight/pipeline/ (the app
    # already serves the member freight calculator at /freight/).
    path("pipeline/", views_freight.freight_board, name="freight_board"),
    path("pipeline/config/", views_freight.freight_config, name="freight_config"),
    path("pipeline/open/", views_freight.freight_open, name="freight_open"),
    path("pipeline/<int:pk>/", views_freight.freight_detail, name="freight_detail"),
    path("pipeline/<int:pk>/line/", views_freight.freight_line_add, name="freight_line_add"),
    path("pipeline/line/<int:line_pk>/edit/", views_freight.freight_line_edit, name="freight_line_edit"),
    path("pipeline/line/<int:line_pk>/remove/", views_freight.freight_line_remove, name="freight_line_remove"),
    path("pipeline/line/<int:line_pk>/receive/", views_freight.freight_receive, name="freight_receive"),
    path("pipeline/<int:pk>/assign/", views_freight.freight_assign, name="freight_assign"),
    path("pipeline/<int:pk>/assign-haul/", views_freight.freight_assign_haul, name="freight_assign_haul"),
    path("pipeline/<int:pk>/unassign/", views_freight.freight_unassign, name="freight_unassign"),
    path("pipeline/<int:pk>/depart/", views_freight.freight_depart, name="freight_depart"),
    path("pipeline/<int:pk>/eta/", views_freight.freight_eta, name="freight_eta"),
    path("pipeline/<int:pk>/arrive/", views_freight.freight_arrive, name="freight_arrive"),
    path("pipeline/<int:pk>/cancel/", views_freight.freight_cancel, name="freight_cancel"),
]
