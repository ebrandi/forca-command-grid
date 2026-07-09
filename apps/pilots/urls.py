from django.urls import path

from . import views

app_name = "pilots"

urlpatterns = [
    path("briefing/", views.briefing, name="briefing"),
    path("contributions/", views.contributions, name="contributions"),
    path("hall-of-fame/", views.hall_of_fame, name="hall_of_fame"),
    path("recognition/toggle/", views.toggle_recognition, name="toggle_recognition"),
    path("idle-queue-nudge/toggle/", views.toggle_idle_queue_nudge, name="toggle_idle_queue_nudge"),
]
