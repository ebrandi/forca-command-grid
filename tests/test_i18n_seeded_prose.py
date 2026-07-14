"""Seam A — the render-time seam for English prose that is SEEDED INTO THE DATABASE.

Three sinks ship English prose *into rows*: the raffle contest templates' ``config``
JSON (objective + prize-ladder names), the PI planet types' ``best_for``/``blurb``, and
the built-in ``DoctrineCategory.label`` ("IMPORTED").

``gettext_lazy`` cannot fix any of them. Django coerces a lazy proxy to ``str`` on
``.save()``, so the English of whatever locale ran the seeder is frozen into the column
and nothing ever translates; inside a ``JSONField`` a proxy is not even serialisable — it
is a hard ``TypeError``. So the columns keep canonical **English** (the fallback and the
audit record) and translation happens at *render* time, keyed on a stable identifier the
row already carries: ``RaffleContestTemplate.key`` / ``RaffleContest.template_key``,
``PiPlanetType.slug``, ``DoctrineCategory.key``.

Each sink is pinned three ways:

* **translated** — a row still holding the shipped English renders in the active locale;
* **verbatim** — a row whose text a leader has EDITED, or whose key the catalogue does not
  know, renders its stored English unchanged in every locale (never blank, never guessed);
* **immutable** — rendering in German never writes German back into the row.

The shipped ``de`` catalogue has no msgstr for these msgids yet, so ``translation.override``
alone would return the English msgid and prove nothing. ``_translated_de`` seeds the msgstrs
into the active catalogue — exactly what a translator filling in the .po entry does — which
is what makes the seam observable now and keeps it from regressing later. (Same approach as
``tests/test_campaigns_services.py``.)
"""
from __future__ import annotations

import contextlib
import json

import pytest
from django.utils import translation

from apps.doctrines import services as doctrine_services
from apps.doctrines.category_i18n import IMPORTED_CATEGORY_KEY
from apps.doctrines.models import DoctrineCategory
from apps.planetary.models import PiPlanetType
from apps.planetary.static_data import PLANET_TYPES, PLANET_TYPES_BY_SLUG
from apps.raffle.contest_templates import (
    BUILTIN,
    RANK_NAMES,
    apply_template,
    seed_builtin_templates,
)
from apps.raffle.models import RaffleContestTemplate
from tests._raffle_utils import make_contest

_MISSING = object()


@contextlib.contextmanager
def _translated_de(**msgstrs):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue."""
    from django.utils.translation import trans_real

    with translation.override("de"):
        # ``_catalog`` is Django's TranslationCatalog (a chain of dicts), so write through
        # __setitem__ — its ``update()`` takes a whole translation object, not a mapping.
        catalog = trans_real.catalog()._catalog
        saved = {k: catalog.get(k, _MISSING) for k in msgstrs}
        for key, value in msgstrs.items():
            catalog[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is _MISSING:
                    catalog._catalogs[0].pop(key, None)
                else:
                    catalog[key] = value


# ===========================================================================
#  The catalogue is code-side and the seeded values stay plain `str`
# ===========================================================================
def test_seeded_prose_is_never_a_lazy_proxy():
    """The seeded values must be real ``str``, not ``gettext_lazy`` proxies.

    This is the whole reason Seam A exists. A proxy in ``RaffleContestTemplate.config``
    (a JSONField) raises TypeError at save/migrate time; a proxy on a CharField is
    silently coerced under the seeding locale and freezes that language into the row.
    ``gettext_noop`` marks the literal for xgettext and returns it unchanged.
    """
    for template in BUILTIN:
        config = template["config"]
        assert type(config["contest"].get("objective", "")) is str
        for prize in config["prizes"]:
            assert type(prize["name"]) is str
        # The clincher: the whole config must survive JSON serialisation.
        json.dumps(config)

    for planet in PLANET_TYPES:
        assert type(planet["best_for"]) is str
        assert type(planet["blurb"]) is str

    assert all(type(name) is str for name in RANK_NAMES)


# ===========================================================================
#  Sink 1 — raffle: objective + prize-rank names inside the config JSONField
# ===========================================================================
@pytest.mark.django_db
def test_raffle_seeded_objective_and_prizes_translate_at_render_time():
    seed_builtin_templates()
    tpl = RaffleContestTemplate.objects.get(key="pvp_activity")

    # The template row itself: English in the DB, translated through the seam.
    assert tpl.config["contest"]["objective"] == "Get pilots on more kills."
    assert tpl.config["prizes"][0]["name"] == "1st prize"

    contest = make_contest()
    assert apply_template(contest, "pvp_activity", overwrite_prizes=True)
    contest.refresh_from_db()
    prize = contest.prizes.get(rank=1)

    # English is unchanged (the default locale renders exactly what it renders today).
    assert contest.template_key == "pvp_activity"
    assert contest.objective == "Get pilots on more kills."
    assert contest.objective_i18n == "Get pilots on more kills."
    assert prize.name == "1st prize"
    assert prize.name_i18n == "1st prize"

    with _translated_de(**{
        "Get pilots on more kills.": "Bringt Piloten auf mehr Kills.",
        "1st prize": "1. Preis",
    }):
        assert contest.objective_i18n == "Bringt Piloten auf mehr Kills."
        assert contest.prizes.get(rank=1).name_i18n == "1. Preis"
        # The template row (the JSONField sink) renders through the same seam.
        assert tpl.objective_i18n == "Bringt Piloten auf mehr Kills."
        assert tpl.prize_names_i18n[0] == "1. Preis"

        # …and the DB still holds canonical English — rendering never writes back.
        contest.refresh_from_db()
        assert contest.objective == "Get pilots on more kills."
        assert RaffleContestTemplate.objects.get(
            key="pvp_activity"
        ).config["contest"]["objective"] == "Get pilots on more kills."


@pytest.mark.django_db
def test_raffle_edited_or_unknown_key_renders_stored_text_verbatim():
    """Leader content is never translated, and an unknown key never blanks a row."""
    seed_builtin_templates()

    # (a) Built from a template, then EDITED by a leader: their words, every locale.
    edited = make_contest(name="Edited")
    apply_template(edited, "pvp_activity", overwrite_prizes=True)
    edited.objective = "Get pilots on more kills, and bring a logi."
    edited.save(update_fields=["objective", "updated_at"])
    renamed = edited.prizes.get(rank=1)
    renamed.name = "Vargur + fit"
    renamed.save(update_fields=["name", "updated_at"])

    # (b) Never built from a built-in template at all (blank template_key).
    handrolled = make_contest(name="Hand rolled", objective="Get pilots on more kills.")

    # (c) A template_key the catalogue does not know (e.g. a leader-authored template).
    unknown = make_contest(name="Unknown", objective="Get pilots on more kills.",
                           template_key="corp_special")

    with _translated_de(**{
        "Get pilots on more kills.": "Bringt Piloten auf mehr Kills.",
        "1st prize": "1. Preis",
        # Even if a translator somehow had entries for the edited text, they must not win.
        "Get pilots on more kills, and bring a logi.": "NIEMALS",
        "Vargur + fit": "NIEMALS",
    }):
        assert edited.objective_i18n == "Get pilots on more kills, and bring a logi."
        assert renamed.name_i18n == "Vargur + fit"
        # Blank / unknown key: the stored English, verbatim — not translated, not blank.
        assert handrolled.objective_i18n == "Get pilots on more kills."
        assert unknown.objective_i18n == "Get pilots on more kills."
        assert all(c.objective_i18n for c in (edited, handrolled, unknown))


# ===========================================================================
#  Sink 2 — planetary: PiPlanetType.best_for / .blurb, keyed on `slug`
# ===========================================================================
def _seed_planet_types():
    """Exactly what ``manage.py load_pi_static`` writes for the planet-type rows."""
    for p in PLANET_TYPES:
        PiPlanetType.objects.update_or_create(
            type_id=p["type_id"],
            defaults={"slug": p["slug"], "name": p["name"], "best_for": p["best_for"],
                      "blurb": p["blurb"], "order": p["order"]},
        )


@pytest.mark.django_db
def test_planet_blurbs_translate_but_the_ccp_planet_name_never_does():
    _seed_planet_types()
    barren = PiPlanetType.objects.get(slug="barren")

    assert barren.best_for == "Robotics & electronics feedstock."
    assert barren.best_for_i18n == "Robotics & electronics feedstock."

    with _translated_de(**{
        "Robotics & electronics feedstock.": "Rohstoffe für Robotik & Elektronik.",
        "Reliable all-rounder rich in metals and organics. Backbone of most "
        "electronics and robotics chains.":
            "Zuverlässiger Allrounder, reich an Metallen und Organik.",
        # A translator must never be able to rename CCP's planet type through us.
        "Barren": "Öde",
    }):
        assert barren.best_for_i18n == "Rohstoffe für Robotik & Elektronik."
        assert barren.blurb_i18n == (
            "Zuverlässiger Allrounder, reich an Metallen und Organik."
        )
        # EVE game data: the planet TYPE name is protected and is not part of the seam.
        assert barren.name == "Barren"

        barren.refresh_from_db()
        assert barren.best_for == "Robotics & electronics feedstock."  # DB stays English


@pytest.mark.django_db
def test_planet_edited_or_unknown_slug_renders_stored_text_verbatim():
    _seed_planet_types()

    edited = PiPlanetType.objects.get(slug="lava")
    edited.best_for = "Construction metals — ask Logistics before you commit."
    edited.blurb = ""  # a leader can legitimately clear it
    edited.save(update_fields=["best_for", "blurb"])

    # A slug the code-side catalogue has never heard of (e.g. a future CCP planet).
    unknown = PiPlanetType.objects.create(
        type_id=99999, slug="shattered", name="Shattered", order=9,
        best_for="Construction metals & heavy industry.", blurb="Not in the catalogue.")

    with _translated_de(**{
        "Construction metals & heavy industry.": "Baumetalle & Schwerindustrie.",
        "Construction metals — ask Logistics before you commit.": "NIEMALS",
        "Not in the catalogue.": "NIEMALS",
    }):
        # Edited row: the leader's English, verbatim.
        assert edited.best_for_i18n == "Construction metals — ask Logistics before you commit."
        assert edited.blurb_i18n == ""  # blank in, blank out — never invented
        # Unknown slug: verbatim, even though the *text* matches a catalogue msgid.
        assert unknown.best_for_i18n == "Construction metals & heavy industry."
        assert unknown.blurb_i18n == "Not in the catalogue."


def test_planet_catalogue_covers_every_seeded_slug():
    """The seam is keyed on ``slug``; a typo there would silently disable translation."""
    assert set(PLANET_TYPES_BY_SLUG) == {p["slug"] for p in PLANET_TYPES}
    assert len(PLANET_TYPES_BY_SLUG) == 8


# ===========================================================================
#  Sink 3 — doctrines: DoctrineCategory.label, keyed on `key`
# ===========================================================================
@pytest.mark.django_db
def test_imported_category_label_translates_at_render_time():
    cat = doctrine_services.imported_category()

    assert cat.key == IMPORTED_CATEGORY_KEY
    assert cat.label == "IMPORTED"
    assert cat.label_i18n == "IMPORTED"

    with _translated_de(**{"IMPORTED": "IMPORTIERT"}):
        assert cat.label_i18n == "IMPORTIERT"
        cat.refresh_from_db()
        assert cat.label == "IMPORTED"  # the column keeps canonical English

    # Re-seeding under a German request must not write German into the row.
    with _translated_de(**{"IMPORTED": "IMPORTIERT"}):
        again = doctrine_services.imported_category()
        assert again.pk == cat.pk
        assert again.label == "IMPORTED"


@pytest.mark.django_db
def test_doctrine_category_edited_or_unknown_key_renders_verbatim():
    # The built-in row is seeded by migration 0003; a leader renames it in the admin.
    renamed = doctrine_services.imported_category()
    renamed.label = "From ESI"
    renamed.save(update_fields=["label"])
    leader_made = DoctrineCategory.objects.create(
        key="capitals", label="IMPORTED", sort_order=1)

    with _translated_de(**{"IMPORTED": "IMPORTIERT", "From ESI": "NIEMALS"}):
        # Built-in key, but the label was edited → the leader's text, verbatim.
        assert renamed.label_i18n == "From ESI"
        # Unknown key, even though the label text collides with the built-in msgid.
        assert leader_made.label_i18n == "IMPORTED"
        assert leader_made.label_i18n  # never blank
