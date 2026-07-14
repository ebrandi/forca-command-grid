"""Code-defined, gettext-wrapped scaffolds for persisted evidence claims (Seam B).

``CandidateEvidence.claim`` is prose that is BUILT BY A WRITER AND READ BY SOMEONE ELSE. The
writer is usually the ``recruitment.refresh_evidence`` Celery task — a worker with no request,
no user and therefore no locale. So the naive fix (wrap the f-string in ``gettext``) cannot
work here:

* Django coerces a ``gettext_lazy`` proxy to ``str`` on ``.save()``. The row would be frozen in
  whichever locale the *writer* happened to have (the worker: English), and every reader — a
  German recruiter included — would see that same frozen string forever. Such a ``_()`` sails
  through ``makemessages`` and translates precisely nothing.
* ``_(f"…")`` is worse still: ``xgettext`` cannot see through an f-string, so the msgid never
  even enters the catalogue. It is a silent no-op. There must be none in this module — every
  msgid below is a literal with ``%(name)s`` placeholders.

So the sentence is not persisted translated. What is persisted is a stable ``claim_key`` plus a
plain-JSON ``claim_params`` dict, alongside the English prose (which stays: it is the fallback,
the audit record, and what legacy keyless rows render from). The reader resolves the msgid under
their OWN locale via :func:`render_text`, called from ``CandidateEvidence.claim_i18n``.

The i18n boundary lands between the scaffold sentence and its substituted values: a param is
NEVER translated. That is deliberate — the params here carry EVE game data (corp role names such
as ``Director``, ``Station_Manager``) and pre-formatted numerics, all of which stay canonical
English by policy. A sentence *containing* one is still translatable prose, which is exactly what
these msgids are.

``_FLAG_ROLE`` in ``services`` ("Director") is compared against ESI data — it is a lookup value,
never a msgid, and must never be translated.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# Keyed by the value persisted in ``CandidateEvidence.claim_key``. Keys are namespaced by the
# evidence theme they serve and are STABLE: a stored row references one forever, so renaming a key
# silently demotes every existing row to its English fallback. Add, never rename.
#
# Each value is a gettext msgid with named ``%(param)s`` placeholders. Named (not positional) so a
# translator may reorder them freely — German and Portuguese both need to.
SCAFFOLDS: dict[str, str] = {
    # -- public evidence (build_public_evidence; written by the Celery beat) ------------------
    "public.character_age": _("Character age: %(years)s years"),
    "public.security_status": _("Security status: %(value)s"),
    "public.corp_churn": _("%(count)s corporation(s) in the last 12 months"),
    # The flagged variant is a SEPARATE msgid, not the plain one plus a translated suffix:
    # concatenating two msgids gives a translator no way to reorder or re-case the join, and
    # several languages need to. One key per whole sentence.
    "risk.corp_churn_flagged": _(
        "%(count)s corporation(s) in the last 12 months — worth asking about"
    ),
    "risk.red_standing": _(
        "Flew with %(count)s corp(s) we hold red — verify before accepting"
    ),
    # -- ESI-derived evidence (build_esi_evidence; written on the consent callback) -----------
    # %(total_sp)s arrives PRE-FORMATTED ("4.2B", "550K") — the abbreviation is computed at the
    # write site so the msgid carries no format spec a translator could break.
    "combat.total_sp": _("Total skill points: %(total_sp)s (ESI-confirmed)"),
    "combat.skills_trained": _("%(count)s skills trained, %(at_level_v)s at level V"),
    # %(roles)s is the raw ESI corp-role list — EVE game data, stays English (protected).
    "roles.held": _("Holds roles in their current corp: %(roles)s"),
    "roles.held_director": _(
        "Holds roles in their current corp: %(roles)s — currently a Director; "
        "confirm why they are leaving"
    ),
    "roles.none": _("No special roles in their current corp (line member)"),
}


def render_text(key: str, params: dict | None, fallback: str) -> str:
    """The claim for ``key`` under the READER's active locale — or ``fallback``.

    This is the read side of the seam. ``str(...)`` forces the ``gettext_lazy`` proxy to resolve
    *now*, against whatever catalogue is active for the reader (their request's language), rather
    than at write time in the worker.

    Never blanks and never raises. A keyless legacy row, a key from a newer/older deploy, or a
    params dict missing a placeholder all degrade to the stored English prose — which every row,
    old and new, has.
    """
    if not key:
        return fallback
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return fallback
    try:
        return str(scaffold) % (params or {})
    except (KeyError, TypeError, ValueError):
        return fallback


def english_text(key: str, params: dict | None = None) -> str:
    """The msgid for ``key`` interpolated with ``params``, in **source English**.

    ``translation.override(None)`` deactivates translation entirely, so ``str(proxy)`` yields the
    msgid verbatim instead of the active catalogue's msgstr. This is what keeps the persisted
    ``claim`` column locale-independent: a row written while a German recruiter's request is
    active stores the same English bytes as the Celery beat writes. Building the prose column
    *through* the scaffold (rather than repeating the f-string beside it) also makes msgid/prose
    drift structurally impossible.
    """
    from django.utils import translation

    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return ""
    with translation.override(None):
        return str(scaffold) % (params or {})
