from __future__ import annotations

from django.urls import path

from . import views

app_name = "skills"

urlpatterns = [
    path("", views.my_plans, name="plans"),
    path("import/", views.import_mine, name="import_mine"),
    path("gap/", views.skill_gap, name="gap"),
    path("create/", views.create_plan, name="create"),
    path("<int:pk>/", views.plan_detail, name="detail"),
    path("<int:pk>/steps/<int:step_id>/toggle/", views.toggle_step, name="toggle_step"),
    path("<int:pk>/steps/<int:step_id>/remove/", views.remove_step, name="remove_step"),
    path("<int:pk>/steps/<int:step_id>/move/", views.move_step, name="move_step"),
    path("<int:pk>/delete/", views.delete_plan, name="delete"),
    path("<int:pk>/export/", views.export_plan, name="export"),
]
