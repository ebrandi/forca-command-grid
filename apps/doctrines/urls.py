from __future__ import annotations

from django.urls import path

from . import views

app_name = "doctrines"

urlpatterns = [
    path("", views.doctrine_list, name="list"),
    path("coverage/", views.coverage_dashboard, name="coverage"),
    path("ships/", views.doctrine_ships, name="ships"),
    path("my-readiness/", views.my_readiness, name="my_readiness"),
    path("fits/<int:fit_id>/export/", views.fit_export, name="fit_export"),
    path("<int:pk>/", views.doctrine_detail, name="detail"),
    path("<int:pk>/readiness/", views.doctrine_readiness, name="readiness"),
    path("<int:pk>/prep/", views.doctrine_prep, name="prep"),
    path("<int:pk>/supply/", views.doctrine_supply, name="supply"),
    path("supply/task/", views.supply_task, name="supply_task"),
]
