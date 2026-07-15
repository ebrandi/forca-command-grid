"""Render-time i18n seam for the seeded combat-rank titles/descriptions (Seam A).

A :class:`~apps.killboard.models.CombatRankTitle` row's ``name`` and ``description`` are
**seeded into the database** — migration ``0009`` writes the 17-rung ``DEFAULT_LADDER``.
The static fallback ladders in :mod:`apps.killboard.ranks` (``_FALLBACK_LADDERS``) and the
legacy ``leaderboards.RANK_LADDER`` are the *live render path* for any metric with no active
rows (the solo/final-blow/active-day support tracks, and a bare/test database), plus the
hard-coded ``"Capsuleer"`` fallback in :func:`apps.killboard.ranks.combat_rank`.

A ``gettext_lazy`` proxy cannot help for the DB rows: Django coerces it to ``str`` on
``.save()``, so whatever locale was active when the ladder was seeded would be frozen into the
column forever. So the English stays plain ``str`` in the DB (the canonical value, the audit
record and the fallback), marked for extraction with ``gettext_noop`` — Django's
``makemessages`` passes ``--keyword=gettext_noop``, so ``xgettext`` sees it exactly as it sees
``_()`` — and it is translated at *render* time by :func:`rank_title_for` /
:func:`rank_description_for`.

Leader-renamed ranks are corp content: a title an officer edited to their own words is no
longer one of the shipped English strings, so it is returned verbatim in every locale. The
stored value is always the floor, so this can never blank a rank. "Translate until edited",
keyed on the shipped English itself — there is no separate stable slug/threshold that is
unambiguous across both the seeded DB ladder and the static fallbacks (they reuse thresholds),
and a rank name should translate to the same word wherever it appears.
"""
from __future__ import annotations

from django.utils.translation import gettext, gettext_noop

# Every combat-rank TITLE the code ships as English, marked for xgettext. Membership here is
# the "is this still the shipped built-in?" test read by :func:`rank_title_for`.
SEED_TITLES: frozenset[str] = frozenset({
    # migration 0009 DEFAULT_LADDER (metric = kills)
    gettext_noop("Dockside Recruit"),
    gettext_noop("First Blood"),
    gettext_noop("Skirmisher"),
    gettext_noop("Line Pilot"),
    gettext_noop("Combat Wingman"),
    gettext_noop("Proven Combatant"),
    gettext_noop("Battle-Tested Pilot"),
    gettext_noop("Fleet Regular"),
    gettext_noop("Veteran Combatant"),
    gettext_noop("Ace Pilot"),
    gettext_noop("Elite Ace"),
    gettext_noop("Vanguard Hunter"),
    gettext_noop("War Machine"),
    gettext_noop("Command Grid Enforcer"),
    gettext_noop("FORCA Warlord"),
    gettext_noop("Campaign Legend"),
    gettext_noop("Immortal of FORCA"),
    # ranks._FALLBACK_LADDERS["kills"] + leaderboards.RANK_LADDER + combat_rank's "Capsuleer"
    gettext_noop("Capsuleer"),
    gettext_noop("Recruit"),
    gettext_noop("Hunter"),
    gettext_noop("Killer"),
    gettext_noop("Marauder"),
    gettext_noop("Warlord"),
    gettext_noop("Apex Predator"),
    # ranks._FALLBACK_LADDERS["solo_kills"]
    gettext_noop("Wingman"),
    gettext_noop("Lone Wolf"),
    gettext_noop("Duelist"),
    gettext_noop("Solo Hunter"),
    gettext_noop("Nomad"),
    gettext_noop("Ghost"),
    # ranks._FALLBACK_LADDERS["final_blows"]
    gettext_noop("Trigger"),
    gettext_noop("Finisher"),
    gettext_noop("Executioner"),
    gettext_noop("Closer"),
    gettext_noop("Reaper"),
    # ranks._FALLBACK_LADDERS["active_days"]
    gettext_noop("Visitor"),
    gettext_noop("Regular"),
    gettext_noop("Committed"),
    gettext_noop("Veteran"),
    gettext_noop("Ever-Present"),
    gettext_noop("Pillar"),
})

# Every combat-rank DESCRIPTION the migration seeds (DEFAULT_LADDER), marked for xgettext.
SEED_DESCRIPTIONS: frozenset[str] = frozenset({
    gettext_noop("Every legend starts here. Undock and make your mark."),
    gettext_noop("Your first confirmed kill — welcome to the fight."),
    gettext_noop("Five down. You're finding your range."),
    gettext_noop("A dependable body on the field."),
    gettext_noop("Reliable in a gang — the corp counts on you."),
    gettext_noop("Fifty kills of proof you belong on grid."),
    gettext_noop("A hundred kills. The enemy knows your name."),
    gettext_noop("Always on the fleet, always in the mix."),
    gettext_noop("Five hundred kills of hard-won experience."),
    gettext_noop("Four digits. A genuine corp asset."),
    gettext_noop("Among the sharpest blades in the corp."),
    gettext_noop("You lead from the front of every roam."),
    gettext_noop("Five thousand kills. A one-pilot problem."),
    gettext_noop("The grid is yours to hold."),
    gettext_noop("Ten thousand kills. A pillar of corp history."),
    gettext_noop("Your name is written across a decade of wars."),
    gettext_noop("The summit. Almost no one will ever stand here."),
})


def rank_title_for(stored: str) -> str:
    """The title to *display* for a rank: the shipped English translated, else verbatim.

    Translated only while the stored title is still one of the built-in English strings; a
    leader's renamed rank (not in :data:`SEED_TITLES`) is returned verbatim in every locale.
    """
    return gettext(stored) if stored and stored in SEED_TITLES else stored


def rank_description_for(stored: str) -> str:
    """The description to *display* for a rank: the seeded English translated, else verbatim."""
    return gettext(stored) if stored and stored in SEED_DESCRIPTIONS else stored
