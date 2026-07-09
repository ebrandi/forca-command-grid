from django.urls import path

from . import views

app_name = "kb"

urlpatterns = [
    path("", views.kb_list, name="list"),
    path("new/", views.kb_edit, name="new"),
    path("new/save/", views.kb_save, name="create"),
    path("<slug:slug>/", views.kb_detail, name="detail"),
    path("<slug:slug>/edit/", views.kb_edit, name="edit"),
    path("<slug:slug>/save/", views.kb_save, name="save"),
    path("<slug:slug>/delete/", views.kb_delete, name="delete"),
]
