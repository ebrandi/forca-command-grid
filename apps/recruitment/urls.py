from django.urls import path

from . import views

app_name = "recruitment"

urlpatterns = [
    path("", views.candidate_list, name="list"),
    path("add/", views.candidate_add, name="add"),
    path("<int:pk>/", views.candidate_detail, name="detail"),
    path("<int:pk>/refresh/", views.candidate_refresh, name="refresh"),
    path("<int:pk>/update/", views.candidate_update, name="update"),
    path("<int:pk>/consent/", views.request_consent, name="request_consent"),
    # Candidate-facing live ESI link (public; the second EVE application).
    path("oauth/begin/<str:state>/", views.oauth_begin, name="oauth_begin"),
    path("oauth/callback/", views.oauth_callback, name="oauth_callback"),
]
