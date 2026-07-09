from django.urls import path

from . import views

app_name = "corporation"

urlpatterns = [
    path("", views.roster_view, name="roster"),
    path("sync/", views.sync_roster, name="sync"),
    path("finance/", views.finance, name="finance"),
    path("finance/sync/", views.sync_finance, name="finance_sync"),
    path("income/", views.income, name="income"),
    path("standings/", views.standings, name="standings"),
    path("standings/sync/", views.sync_contacts, name="standings_sync"),
    path("extractions/", views.extractions, name="extractions"),
    path("extractions/sync/", views.sync_extractions, name="extractions_sync"),
    path("structures/", views.structures, name="structures"),
    path("structures/sync/", views.sync_structures, name="structures_sync"),
    path("infrastructure/", views.infrastructure, name="infrastructure"),
]
