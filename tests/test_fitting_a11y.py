"""Tocha's Lab accessibility harness.

A dependency-free static a11y lint: render each page with the test client, isolate the
``<main>`` content region (so base chrome is out of scope), and assert the WCAG basics that
regress most easily in a template edit — one ``<h1>`` per page, an ``alt`` on every image, an
accessible name on every text control, and a discernible label on every button. Content
inside an inert ``<template>`` (Alpine clones it client-side) is skipped, matching what a
screen reader actually encounters on load.
"""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from django.urls import reverse

from apps.fitting import services

from ._fitting_utils import make_member, seed_dogma

pytestmark = pytest.mark.django_db

# Input types that carry their own name (value/no text entry) — a label is not required.
_SELF_LABELLING = {"hidden", "submit", "reset", "button", "image"}


class _A11yScanner(HTMLParser):
    """Collect the facts needed to judge a page's content region against WCAG basics."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1_count = 0
        self.images_without_alt = 0
        self.label_for: set[str] = set()
        self.controls: list[dict] = []      # {tag, id, aria, type}
        self.unnamed_buttons = 0
        self._template_depth = 0
        self._button_stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "template":
            self._template_depth += 1
            return
        if self._template_depth:                       # inert until cloned — skip
            if tag == "button":
                self._button_stack.append({"named": True, "text": ""})
            return
        if tag == "h1":
            self.h1_count += 1
        elif tag == "img":
            if "alt" not in a:
                self.images_without_alt += 1
        elif tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            if not (tag == "input" and a.get("type", "").lower() in _SELF_LABELLING):
                self.controls.append({
                    "tag": tag, "id": a.get("id", ""),
                    "aria": bool(a.get("aria-label") or a.get("aria-labelledby")),
                })
        elif tag == "button":
            named = bool(a.get("aria-label") or a.get("title"))
            self._button_stack.append({"named": named, "text": ""})

    def handle_endtag(self, tag):
        if tag == "template" and self._template_depth:
            self._template_depth -= 1
            return
        if tag == "button" and self._button_stack:
            b = self._button_stack.pop()
            if not self._template_depth and not b["named"] and not b["text"].strip():
                self.unnamed_buttons += 1

    def handle_data(self, data):
        if self._button_stack and data.strip():
            self._button_stack[-1]["text"] += data

    # --- assertions -------------------------------------------------------- #
    def unnamed_controls(self) -> list[dict]:
        return [c for c in self.controls
                if not c["aria"] and not (c["id"] and c["id"] in self.label_for)]


def _scan_main(html: str) -> _A11yScanner:
    """Scan only the ``<main>`` content region — base nav/footer chrome is out of scope."""
    start = html.find("<main")
    end = html.find("</main>", start)
    assert start != -1 and end != -1, "page has no <main> content region"
    scanner = _A11yScanner()
    scanner.feed(html[start:end])
    return scanner


def _assert_accessible(html: str):
    s = _scan_main(html)
    assert s.h1_count == 1, f"expected exactly one <h1>, found {s.h1_count}"
    assert s.images_without_alt == 0, f"{s.images_without_alt} <img> without alt"
    unnamed = s.unnamed_controls()
    assert not unnamed, f"controls with no accessible name: {unnamed}"
    assert s.unnamed_buttons == 0, f"{s.unnamed_buttons} button(s) with no discernible label"


@pytest.fixture
def officer(db):
    from core import rbac
    seed_dogma()
    return make_member("eve:2300", 2300, "A11y Officer", role=rbac.ROLE_OFFICER)


def test_index_page_is_accessible(client, officer):
    from ._fitting_utils import RIFTER
    services.create_fit(officer, name="A11y Rifter", ship_type_id=RIFTER, items=[])
    client.force_login(officer)
    resp = client.get(reverse("fitting:index"))
    assert resp.status_code == 200
    _assert_accessible(resp.content.decode())


def test_detail_page_is_accessible(client, officer):
    """The editor renders its richest control set for an officer-owner with a shortfall +
    a supplier (rename, supply buttons, supplier select, doctrine promote, share)."""
    from apps.procurement.models import Supplier

    from ._fitting_utils import EFT, RIFTER
    Supplier.objects.create(kind=Supplier.Kind.HUB, display_name="Jita Seller",
                            status=Supplier.Status.ACTIVE)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(officer, name="A11y Editor", ship_type_id=RIFTER,
                              items=parsed["items"])
    client.force_login(officer)
    resp = client.get(reverse("fitting:detail", args=[fit.pk]))
    assert resp.status_code == 200
    assert resp.context["stock"]["missing"] and resp.context["suppliers"]
    _assert_accessible(resp.content.decode())
