"""Tocha's Lab routes. The URL prefix (/lab/) and namespace stay generic; the brand
"Tocha's Lab" lives only in templates and localised strings."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "fitting"

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("import/eft/", views.import_eft, name="import_eft"),
    path("import/killmail/<int:killmail_id>/", views.import_killmail, name="import_killmail"),
    path("import/doctrine/<int:fit_id>/", views.import_doctrine, name="import_doctrine"),
    path("telemetry/", views.telemetry, name="telemetry"),
    path("search/modules/", views.search_modules, name="search_modules"),
    path("search/hulls/", views.search_hulls, name="search_hulls"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/save/", views.save, name="save"),
    path("<int:pk>/fork/", views.fork, name="fork"),
    path("<int:pk>/share/", views.share, name="share"),
    path("<int:pk>/unshare/", views.unshare, name="unshare"),
    path("<int:pk>/delete/", views.delete, name="delete"),
    path("<int:pk>/export.eft", views.export_eft, name="export_eft"),
    path("<int:pk>/training.txt", views.training_export, name="training_export"),
    path("<int:pk>/compare/", views.compare, name="compare"),
    path("<int:pk>/promote/", views.promote, name="promote"),
    path("s/<str:token>/", views.shared, name="shared"),
]
