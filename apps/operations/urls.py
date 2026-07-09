from django.urls import path

from . import views

app_name = "operations"

urlpatterns = [
    path("", views.op_list, name="list"),
    path("timers/", views.timer_board, name="timers"),
    path("sov/", views.sov_board, name="sov"),
    path("timers/add/", views.timer_add, name="timer_add"),
    path("timers/<int:pk>/remove/", views.timer_remove, name="timer_remove"),
    path("ship-search/", views.ship_search, name="ship_search"),
    path("analytics/cancellations/", views.op_cancellation_analytics, name="cancellation_analytics"),
    path("create/", views.op_create, name="create"),
    # OPS-4 (3.12): recurring op templates (officer). Before <int:pk> so "templates" isn't a pk.
    path("templates/", views.op_templates, name="templates"),
    path("templates/create/", views.op_template_create, name="template_create"),
    path("templates/run/", views.op_template_run, name="template_run"),
    path("templates/<int:pk>/edit/", views.op_template_edit, name="template_edit"),
    path("templates/<int:pk>/toggle/", views.op_template_toggle, name="template_toggle"),
    path("templates/<int:pk>/delete/", views.op_template_delete, name="template_delete"),
    path("<int:pk>/", views.op_detail, name="detail"),
    path("<int:pk>/edit/", views.op_edit, name="edit"),
    path("<int:pk>/tasks/", views.op_generate_tasks, name="generate_tasks"),
    path("<int:pk>/status/", views.op_status, name="status"),
    path("<int:pk>/override/", views.op_override, name="override"),
    path("<int:pk>/announce/", views.op_announce, name="announce"),
    path("<int:pk>/rsvp/", views.op_rsvp, name="rsvp"),
    path("<int:pk>/commit/", views.op_commit, name="commit"),
    path("<int:pk>/uncommit/", views.op_uncommit, name="uncommit"),
    path("<int:pk>/attend/", views.op_attend, name="attend"),
    path("<int:pk>/unattend/", views.op_unattend, name="unattend"),
    path("<int:pk>/attendance/", views.op_attendance_action, name="attendance_action"),
    path("<int:pk>/pull-fleet/", views.op_pull_fleet, name="pull_fleet"),
]
