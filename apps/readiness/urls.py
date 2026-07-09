from django.urls import path

from . import views

app_name = "readiness"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("recompute/", views.recompute, name="recompute"),
    path("gap/task/", views.create_tasks_from_gap, name="gap_task"),
    path("findings/", views.findings_register, name="findings"),
    path("tasks/", views.task_queue, name="task_queue"),
    path("alerts/", views.alerts_log, name="alerts"),
    path("report/", views.weekly_report, name="report"),
    path("timeline/", views.timeline, name="timeline"),
    path("sim/", views.simulator, name="simulator"),
    path("d/<str:key>/", views.dimension_detail, name="dimension"),
    path("kpi/<str:key>/", views.kpi_detail, name="kpi"),
    path("me/", views.pilot_dashboard, name="me"),
    path("me/reco/<int:pk>/", views.reco_action, name="reco_action"),
    path("me/<int:character_id>/", views.pilot_view, name="pilot_view"),
]
