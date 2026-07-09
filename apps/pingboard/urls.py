from django.urls import path

from . import views

app_name = "pingboard"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("calendar/", views.calendar, name="calendar"),
    path("calendar/event/create/", views.event_create, name="event_create"),
    path("calendar/<int:pk>/", views.calendar_event, name="calendar_event"),
    path("calendar/<int:pk>/<str:action>/", views.event_action, name="event_action"),
    path("compose/", views.compose, name="compose"),
    path("history/", views.history, name="history"),
    path("alerts/<int:pk>/", views.alert_detail, name="alert_detail"),
    path("alerts/<int:pk>/<str:action>/", views.alert_action, name="alert_action"),
    path("channels/", views.my_channels, name="my_channels"),
    path("channels/prefs/", views.channel_prefs, name="channel_prefs"),
    path("channels/link/", views.channel_link, name="channel_link"),
    path("channels/confirm/", views.channel_confirm, name="channel_confirm"),
    path("channels/unlink/", views.channel_unlink, name="channel_unlink"),
    # Inbound Telegram webhook (anonymous; secret in the path). Point the bot's
    # webhook at /pingboard/telegram/webhook/<PINGBOARD_TELEGRAM_WEBHOOK_SECRET>/.
    path("telegram/webhook/<str:secret>/", views.telegram_webhook, name="telegram_webhook"),
]
