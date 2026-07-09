from __future__ import annotations

from django.urls import path

from . import views

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
]
