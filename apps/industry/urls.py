from __future__ import annotations

from django.urls import path

from . import tools, views, views_mrp

app_name = "industry"

urlpatterns = [
    # Unified Industry Center hub + tools.
    path("", tools.industry_home, name="home"),
    path("guide/", tools.guide, name="guide"),
    path("calculator/", tools.calculator, name="calculator"),
    path("invention/", tools.invention_planner, name="invention"),
    path("chain/", tools.chain_explorer, name="chain"),
    path("blueprints/", tools.blueprint_browser, name="blueprints"),
    path("jobs/", tools.job_tracker, name="jobs"),
    path("demand/", tools.corp_demand, name="demand"),
    path("demand/create/", tools.plan_from_demand, name="plan_from_demand"),
    # MRP v1 (P3): the Material Plan
    path("mrp/", views_mrp.mrp_board, name="mrp"),
    path("mrp/run/", views_mrp.mrp_run_now, name="mrp_run"),
    path("mrp/req/<int:pk>/action/", views_mrp.mrp_fan_out, name="mrp_fan_out"),
    path("jobs/plan-from-job/", tools.plan_from_job, name="plan_from_job"),
    # Production plans (the board keeps its `board` name for existing links).
    path("plans/", views.project_board, name="board"),
    path("plans/new/", views.project_create, name="create"),
    path("type-search/", views.type_search, name="type_search"),
    path("plans/<int:pk>/", views.project_detail, name="detail"),
    path("plans/<int:pk>/claim/", views.project_claim, name="claim"),
    path("plans/<int:pk>/push-jobs/", views.project_push_jobs, name="push_jobs"),
    path("plans/<int:pk>/status/", views.project_status, name="status"),
    path("plans/<int:pk>/duplicate/", views.project_duplicate, name="duplicate"),
    path("plans/<int:pk>/archive/", views.project_archive, name="archive"),
    path("plans/<int:pk>/items/add/", views.add_item, name="add_item"),
    path("plans/<int:pk>/items/<int:item_id>/remove/", views.remove_item, name="remove_item"),
    path("plans/<int:pk>/recompute/", views.recompute_bom, name="recompute"),
    path("plans/<int:pk>/shopping-list/", views.make_shopping_list, name="shopping_list"),
    path("plans/<int:pk>/reserve/", views.reserve_stock, name="reserve_stock"),
    path("plans/<int:pk>/release/", views.release_stock, name="release_stock"),
]
