from __future__ import annotations

from django.urls import path

from . import views

app_name = "mentorship"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("me/", views.dashboard, name="dashboard"),
    path("register/mentor/", views.register_mentor, name="register_mentor"),
    path("register/mentee/", views.register_mentee, name="register_mentee"),
    path("tracks/", views.tracks, name="tracks"),
    path("tracks/<slug:key>/", views.track_detail, name="track_detail"),
    path("mentors/", views.mentor_directory, name="directory"),
    path("request-mentor/", views.request_mentor, name="request_mentor"),
    path("invite-mentee/", views.invite_mentee, name="invite_mentee"),
    path("pair/<int:pk>/", views.pairing_detail, name="pairing"),
    path("pair/<int:pk>/respond/", views.pairing_respond, name="pairing_respond"),
    path("pair/<int:pk>/action/", views.pairing_action, name="pairing_action"),
    path("pair/<int:pk>/enroll/", views.enroll_track, name="enroll_track"),
    path("pair/<int:pk>/session/", views.session_create, name="session_create"),
    path("task/<int:pk>/action/", views.task_action, name="task_action"),
    path("session/<int:pk>/action/", views.session_action, name="session_action"),
]
