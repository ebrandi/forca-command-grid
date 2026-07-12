"""Per-locale re-render seam (design doc 08 §7.2).

``render_for(alert, lang)`` returns the ``(subject, body)`` for an alert in ``lang``,
re-rendered from the retained ``Alert.template_key`` + ``Alert.context`` instead of the
single frozen ``Alert.body``. It is the one render function shared by the dispatcher
(under ``translation.override(lang)`` in a Celery worker) and the in-app view (on a
request whose language is already ``lang``).

Resolution order (English-safe at every step — no branch can blank/crash a notification):

1. Custom free-text / un-migrated call site -> the frozen ``Alert.title``/``Alert.body``,
   VERBATIM, never translated (D14.6).
2. Code message-scaffold (``messages.SCAFFOLDS``) -> the gettext msgid resolved under the
   active locale, then ``{slot}`` interpolation over the raw context (the translatable
   class, D14.7).
3. DB ``AlertTemplate`` -> corp content, rendered verbatim (same text every locale, D14.8).
4. Nothing resolvable -> the frozen ``Alert.body`` (audit fallback).

The interpolation values in ``alert.context`` NEVER pass through ``gettext`` — the i18n
boundary lands between the scaffold sentence and the substituted EVE/game/user value
(doc 08 §11.1). ``rendering.render`` is reused unchanged, so a translated msgid is
sandboxed exactly like a source body (no ``{a.b}``/``{a[0]}`` traversal, unknown slots
render empty).
"""
from __future__ import annotations

from . import rendering


def render_for(alert, lang: str) -> tuple[str, str]:
    """Return ``(subject, body)`` for ``alert`` in ``lang``.

    Assumes the caller has already entered ``with translation.override(lang)`` (dispatcher)
    or is on a request whose active language is ``lang`` (in-app view). ``lang`` is accepted
    for symmetry/observability; the active catalogue is what ``str(gettext_lazy_msgid)``
    actually resolves against. Never raises into the caller.
    """
    ctx = alert.context or {}

    # 1) Custom free-text / un-migrated call site: verbatim, NEVER translated (D14.6).
    if alert.custom_message or (not alert.template_key and not ctx):
        return alert.title, alert.body

    key = alert.template_key

    # 2) Code message-scaffold (the translatable class): gettext msgid -> {slot} interp.
    from .messages import SCAFFOLDS

    sc = SCAFFOLDS.get(key)
    if sc is not None:
        # str() forces the gettext_lazy proxy to resolve NOW, under the active override.
        subject = rendering.render(str(sc.subject), ctx) if sc.subject else alert.title
        body = rendering.render(str(sc.body), ctx)
        return (subject or alert.title), body

    # 3) DB AlertTemplate: corp content -> verbatim (same text in every locale) (D14.8).
    if key:
        from .models import AlertTemplate

        tpl = AlertTemplate.objects.filter(key=key).first()
        if tpl is not None:
            subject = rendering.render(tpl.subject, ctx) if tpl.subject else alert.title
            return subject, rendering.render(tpl.body, ctx)

    # 4) Nothing resolvable: audit fallback (never blanks/crashes).
    return alert.title, alert.body
