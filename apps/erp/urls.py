from django.urls import path

from . import views

app_name = "erp"

urlpatterns = [
    path("", views.board, name="board"),
    path("jobs/create/", views.create_job, name="create_job"),
    path("jobs/<int:pk>/claim/", views.claim, name="claim"),
    path("jobs/<int:pk>/status/", views.update_status, name="status"),
    path("jobs/<int:pk>/deliver/", views.deliver, name="deliver"),
    path("jobs/<int:pk>/cancel/", views.cancel_job, name="cancel"),
    path("jobs/<int:pk>/edit/", views.edit_job, name="edit"),
    path("blueprints/add/", views.add_blueprint, name="add_blueprint"),
]
