from django.urls import path

from . import views

app_name = "tasks"

urlpatterns = [
    path("", views.board, name="board"),
    path("create/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/claim/", views.claim, name="claim"),
    path("<int:pk>/status/", views.update_status, name="status"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/reassign/", views.reassign, name="reassign"),
]
