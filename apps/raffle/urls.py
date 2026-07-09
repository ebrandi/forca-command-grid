from django.urls import path

from . import views

app_name = "raffle"

urlpatterns = [
    path("", views.home, name="home"),
    path("archive/", views.archive, name="archive"),
    path("outreach/opt-out/", views.outreach_opt_out, name="outreach_opt_out"),
    path("<slug:slug>/", views.detail, name="detail"),
    path("<slug:slug>/me/", views.me, name="me"),
    path("<slug:slug>/draw/", views.transparency, name="transparency"),
]
