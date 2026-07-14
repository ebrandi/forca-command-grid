"""KB services: viewer-scoped embed resolution and visibility checks."""
from __future__ import annotations

import html

from django.utils.translation import gettext as _

from core import rbac

from .models import KbPage


def can_view(user, page: KbPage) -> bool:
    if page.visibility == KbPage.Visibility.PUBLIC:
        return True
    if page.visibility == KbPage.Visibility.OFFICER:
        return rbac.has_role(user, rbac.ROLE_OFFICER)
    return rbac.has_role(user, rbac.ROLE_MEMBER)


def visible_pages(user):
    pages = KbPage.objects.all()
    if rbac.has_role(user, rbac.ROLE_OFFICER):
        return pages
    if rbac.has_role(user, rbac.ROLE_MEMBER):
        return pages.exclude(visibility=KbPage.Visibility.OFFICER)
    return pages.filter(visibility=KbPage.Visibility.PUBLIC)


def _parse_params(raw: str) -> dict:
    out = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _chip(text: str, tone: str = "") -> str:
    cls = {"good": "!border-kill/40 !text-kill", "warn": "!border-loss/40 !text-loss"}.get(tone, "")
    return f'<span class="chip {cls}">{html.escape(text)}</span>'


def _readiness_embed(user, doctrine_name: str | None) -> str:
    from apps.doctrines.models import Doctrine
    from apps.doctrines.services import character_readiness

    if not doctrine_name:
        return _chip(_("readiness: name a doctrine"))
    character = next(
        (c for c in user.characters.all() if c.is_main), user.characters.first()
    )
    if character is None:
        return _chip(_("link a character to see your readiness"))
    doctrine = Doctrine.objects.filter(name__iexact=doctrine_name).prefetch_related(
        "fits__skill_requirements"
    ).first()
    if doctrine is None:
        return _chip(_("unknown doctrine: %(doctrine)s") % {"doctrine": doctrine_name})
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    best = "unknown"
    for fit in doctrine.fits.all():
        s = character_readiness(character, fit).status
        if rank[s] > rank[best]:
            best = s
    label = {
        "optimal": _("You can fly %(doctrine)s (optimal)") % {"doctrine": doctrine.name},
        "viable": _("You can fly %(doctrine)s") % {"doctrine": doctrine.name},
        "not_ready": _("You can't fly %(doctrine)s yet") % {"doctrine": doctrine.name},
        "unknown": _("%(doctrine)s: import skills to check") % {"doctrine": doctrine.name},
    }[best]
    return _chip(label, "good" if best in ("optimal", "viable") else "warn")


def _srp_embed(user) -> str:
    from apps.srp.services import eligible_losses_for

    char_ids = list(user.characters.values_list("character_id", flat=True))
    n = len(eligible_losses_for(char_ids, limit=10))
    if n:
        return _chip(
            _("You have %(count)s loss(es) eligible for SRP") % {"count": n}, "warn"
        )
    return _chip(_("No SRP claims pending"), "good")


def make_resolver(user):
    """Return an embed resolver bound to a viewer (their own data only)."""

    authed = getattr(user, "is_authenticated", False)

    def resolve(name: str, raw: str):
        params = _parse_params(raw)
        if name not in ("readiness", "my-srp", "my_srp"):
            return None
        # Viewer-scoped embeds need a linked pilot; a prospect reading a public page has
        # none, so prompt them to log in rather than crashing on ``user.characters``.
        if not authed:
            return _chip(_("log in to see your own readiness"))
        if name == "readiness":
            return _readiness_embed(user, params.get("doctrine"))
        return _srp_embed(user)

    return resolve
