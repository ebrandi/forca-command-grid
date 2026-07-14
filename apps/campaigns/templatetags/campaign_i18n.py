"""Template access to the built-in blueprint i18n seam (:mod:`apps.campaigns.templates_i18n`).

``{% load campaign_i18n %}`` then ``{% builtin_text objective "title" %}``: renders the *translated*
built-in string while the row still holds the shipped English, and the officer's own words verbatim
the moment they edit it. Never blanks — a corp template or a hand-written row has no catalogue
entry and renders its stored text unchanged.
"""
from __future__ import annotations

from django import template

from apps.campaigns import templates_i18n

register = template.Library()


@register.simple_tag
def builtin_text(obj, field, msgid_field=None):
    """The locale-appropriate text for ``obj.field`` (translate-until-edited)."""
    return templates_i18n.text(obj, field, msgid_field=msgid_field)
