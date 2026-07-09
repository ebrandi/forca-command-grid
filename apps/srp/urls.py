from django.urls import path

from . import views

app_name = "srp"

urlpatterns = [
    path("", views.my_srp, name="mine"),
    path("claim/", views.submit_claim, name="claim"),
    path("queue/", views.queue, name="queue"),
    path("queue/batch-approve/", views.batch_approve, name="batch_approve"),
    path("queue/batch-pay/", views.batch_pay, name="batch_pay"),
    path("budget/", views.budget, name="budget"),
    path("budget/save/", views.budget_save, name="budget_save"),
    path("loss-impact/", views.loss_impact, name="loss_impact"),
    path("settings/", views.settings_view, name="settings"),
    path("rules/add/", views.rule_add, name="rule_add"),
    path("rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("<int:pk>/decide/", views.decide, name="decide"),
    path("<int:pk>/pay/", views.pay, name="pay"),
]
