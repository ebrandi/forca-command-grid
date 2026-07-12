"""Localisation endpoints: the selector POST target and the JS message catalogue.

Mounted at ``/i18n/`` (see config/urls.py). The JS catalogue is served by
Django's runtime ``JavaScriptCatalog`` view (an external response — CSP-clean,
never inlined, honouring the no-``unsafe-inline`` policy; D11).
"""
from __future__ import annotations

from django.urls import path
from django.views.i18n import JavaScriptCatalog

from . import views

urlpatterns = [
    path("setlang/", views.set_language, name="set_language"),
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
]
