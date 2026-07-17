"""URL routes for the procurement surfaces (P4, WS7)."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    # Suppliers
    path("suppliers/", views.suppliers, name="suppliers"),
    path("suppliers/new/", views.supplier_new, name="supplier_new"),
    path("suppliers/<int:pk>/", views.supplier_detail, name="supplier_detail"),
    path("suppliers/<int:pk>/item/", views.supplier_item_add, name="supplier_item_add"),
    # Agreements
    path("agreements/", views.agreements, name="agreements"),
    path("agreements/new/", views.agreement_new, name="agreement_new"),
    path("agreements/<int:pk>/", views.agreement_detail, name="agreement_detail"),
    path("agreements/<int:pk>/line/", views.agreement_line_add, name="agreement_line_add"),
    path("agreements/<int:pk>/action/", views.agreement_action, name="agreement_action"),
    # Purchase orders
    path("pos/", views.pos, name="pos"),
    path("pos/new/", views.po_new, name="po_new"),
    path("pos/<int:pk>/", views.po_detail, name="po_detail"),
    path("pos/<int:pk>/line/", views.po_line_add, name="po_line_add"),
    path("pos/<int:pk>/action/", views.po_action, name="po_action"),
    # Director board
    path("board/", views.board, name="board"),
]
