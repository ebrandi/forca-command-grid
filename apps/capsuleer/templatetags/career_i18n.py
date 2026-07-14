"""Template access to the built-in career-path i18n seam (:mod:`apps.capsuleer.templates_i18n`).

``{% load career_i18n %}`` then ``{% builtin_text milestone "title" %}``: renders the *translated*
built-in string while the row still holds the shipped English, and the pilot's own words verbatim
the moment they edit it. ``{% builtin_text goal "title" msgid_field="name" %}`` covers the goal
title, which is the path's *name* copied at instantiation. Never blanks — a corp template or a
hand-written row has no catalogue entry and renders its stored text unchanged.
"""
from __future__ import annotations

from django import template

from apps.capsuleer import templates_i18n

register = template.Library()


@register.simple_tag
def builtin_text(obj, field, msgid_field=None):
    """The locale-appropriate text for ``obj.field`` (translate-until-edited)."""
    return templates_i18n.text(obj, field, msgid_field=msgid_field)


@register.simple_tag
def builtin_structure_text(source_key, field, stored):
    """Same seam for prose that lives inside ``CareerTemplate.structure`` rather than a column.

    ``source_key`` comes from the key helpers (``ship_key`` / ``knowledge_key`` /
    ``assumption_key``) — the caller knows which part of the JSON it is rendering.
    """
    return templates_i18n.render(source_key, field, stored)
