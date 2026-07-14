"""Display-label maps: the CODE keeps driving the logic, the LABEL is what a human reads.

Some values a pilot sees are not prose — they are codes: ``build``/``buy``, ``Low``/``Medium``/
``High`` complexity, the readiness statuses (``optimal``/``viable``/``not_ready``/``unknown``),
the contribution unit slugs. Every one of them is *compared* — in Python
(``item["complexity"] == "Low"`` awards the "Beginner recommended" badge; ``rank[r.status]``
orders doctrines) and in templates (``{% if node.decision == 'build' %}`` picks the build cost;
``{% if ev.unit == 'isk' %}`` picks the ISK formatter).

Wrapping the value in ``gettext`` would translate it *at source* and silently break every one of
those comparisons for non-English pilots: the badge would vanish, the ISK formatting would fall
through to a plain number, readiness colouring would collapse. So the code stays canonical English
and a parallel ``*_label`` map carries the translation, resolved at render time only.

These tests assert BOTH halves under a real German catalogue: the label is translated **and** the
comparison still fires. A naive "just wrap it" fix passes the first half and fails the second.

``_translated_de`` seeds real msgstrs into the active catalogue (the shipped ``de`` catalogue has
no entry for these yet, so ``translation.override("de")`` alone would hand back the English msgid
and prove nothing) — the same approach as ``tests/test_builtin_template_i18n.py``.
"""
from __future__ import annotations

import contextlib
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.template.loader import get_template
from django.utils import translation

from apps.doctrines.services import FitReadiness, readiness_label
from apps.industry import bom
from apps.pilots.models import unit_label
from apps.planetary.labels import complexity_label, confidence_label

_MISSING = object()


@contextlib.contextmanager
def _translated_de(msgstrs: dict[str, str]):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue."""
    from django.utils.translation import trans_real

    with translation.override("de"):
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


# --------------------------------------------------------------------------- #
#  fixtures (PI static rulebook + prices — same shape as tests/test_planetary.py)
# --------------------------------------------------------------------------- #
@pytest.fixture
def pi_static(db):
    call_command("load_pi_static")
    return True


@pytest.fixture
def pi_priced(pi_static):
    from apps.market.models import MarketPrice
    from apps.planetary.models import PiMaterial

    tier_price = {"P0": 5, "P1": 100, "P2": 1000, "P3": 8000, "P4": 60000}
    MarketPrice.objects.bulk_create([
        MarketPrice(type_id=m.type_id, location=None,
                    profile=MarketPrice.Profile.JITA_SELL,
                    sell_min=Decimal(tier_price[m.tier]),
                    buy_max=Decimal(tier_price[m.tier]) * Decimal("0.9"))
        for m in PiMaterial.objects.all()
    ])
    return True


# =========================================================================== #
#  The headline case: planetary complexity — label translated, badge still fires
# =========================================================================== #
@pytest.mark.django_db
def test_planetary_complexity_label_is_german_and_the_beginner_badge_still_fires(pi_priced):
    """The one a naive fix breaks.

    ``recommend`` awards "Beginner recommended" on ``item["complexity"] == "Low"``. Translate the
    complexity value itself and that comparison never matches in German — the badge silently
    disappears for every non-English pilot. Here the label comes back German *and* the badge is
    still awarded, because the compared value is untouched.
    """
    from apps.planetary import recommend, services

    cfg = services.active_config()

    # Baseline: English is unchanged.
    items_en = recommend.recommend(config=cfg, goal="p0_p1", limit=24)
    beginner_en = [i for i in items_en if any(str(lbl) == "Beginner recommended"
                                              for lbl, _c in i["badges"])]
    assert beginner_en, "fixture must produce at least one beginner-recommended item"
    assert all(i["complexity"] == "Low" for i in beginner_en)
    assert all(str(i["complexity_label"]) == "Low" for i in beginner_en)

    with _translated_de({"Low": "Niedrig", "Beginner recommended": "Für Anfänger empfohlen"}):
        items_de = recommend.recommend(config=cfg, goal="p0_p1", limit=24)
        beginner_de = [i for i in items_de if any(str(lbl) == "Für Anfänger empfohlen"
                                                  for lbl, _c in i["badges"])]

        # Half 1 — the badge still fires: the comparison ``complexity == "Low"`` still matches.
        assert len(beginner_de) == len(beginner_en)

        item = beginner_de[0]
        # Half 2 — the label a human reads IS German…
        assert str(item["complexity_label"]) == "Niedrig"
        # …while the compared value is still the canonical English code.
        assert item["complexity"] == "Low"


@pytest.mark.django_db
def test_planetary_complexity_code_is_never_translated_at_source(pi_priced):
    """Every recommendation carries a raw code from the known set — no locale leaks into it."""
    from apps.planetary import recommend, services

    cfg = services.active_config()
    with _translated_de({"Low": "Niedrig", "Medium": "Mittel", "High": "Hoch"}):
        items = recommend.recommend(config=cfg, goal="max_profit", limit=24)
        assert items
        assert {i["complexity"] for i in items} <= {"Low", "Medium", "High"}


def test_planetary_labels_map_translates_only_the_label():
    with _translated_de({"Low": "Niedrig", "Medium": "Mittel", "High": "Hoch"}):
        assert str(complexity_label("Low")) == "Niedrig"
        assert str(complexity_label("High")) == "Hoch"
        assert str(confidence_label("Medium")) == "Mittel"
        # An unmapped code (the "not costed yet" placeholder) passes through untouched.
        assert str(complexity_label("—")) == "—"


# =========================================================================== #
#  Industry: build / buy
# =========================================================================== #
def test_industry_decision_label_map():
    assert str(bom.decision_label("build")) == "build"
    with _translated_de({"build": "bauen", "buy": "kaufen"}):
        assert str(bom.decision_label("build")) == "bauen"
        assert str(bom.decision_label("buy")) == "kaufen"
        assert str(bom.decision_label("mystery")) == "mystery"


@pytest.mark.django_db
def test_industry_chain_tree_keeps_the_decision_code_and_adds_a_label(priced_sde):
    from apps.industry import calc, chain

    with _translated_de({"build": "bauen", "buy": "kaufen"}):
        tree = chain.chain_tree(600, 1)
        assert tree["decision"] == "build"                     # the compared value: untouched
        assert str(tree["decision_label"]) == "bauen"          # the rendered value: German
        trit = next(c for c in tree["children"] if c["type_id"] == 34)
        assert trit["decision"] == "buy"
        assert str(trit["decision_label"]) == "kaufen"

        d = calc.build_vs_buy(600)
        assert d["decision"] == "build" and str(d["decision_label"]) == "bauen"


@pytest.mark.django_db
def test_industry_chain_node_template_renders_the_label_and_still_picks_the_build_cost(priced_sde):
    """The partial's ``{% if node.decision == 'build' %}`` picks the build cost *and* the cyan
    chip. Render it in German: the chip text is translated, and the build cost (not the buy cost)
    is still the one shown — i.e. the comparison survived."""
    from apps.industry import chain

    tmpl = get_template("industry/_chain_node.html")
    with _translated_de({"build": "bauen", "buy": "kaufen"}):
        tree = chain.chain_tree(600, 1)
        # Two unmistakable, unmistakably different costs so the assertion below cannot pass by
        # accident: only the ``== 'build'`` branch renders 111.
        tree["build_cost"] = Decimal("111")
        tree["buy_cost"] = Decimal("222")
        tree["children"] = []
        html = tmpl.render({"node": tree})

    assert "bauen" in html          # label rendered
    assert "build" not in html      # the raw code is not what the pilot reads
    assert "111" in html and "222" not in html  # the == 'build' branch still fired


# =========================================================================== #
#  Doctrines: readiness status
# =========================================================================== #
def test_doctrine_readiness_label_map_and_status_property():
    r = FitReadiness(fit_id=1, fit_name="Test fit", status="not_ready")
    assert str(r.status_label) == "not ready"
    with _translated_de({"not ready": "nicht bereit", "optimal": "optimal", "viable": "machbar"}):
        assert str(readiness_label("viable")) == "machbar"
        assert str(r.status_label) == "nicht bereit"
        assert r.status == "not_ready"  # the code every ``==``/rank lookup uses: untouched


@pytest.mark.django_db
def test_doctrine_readiness_summary_carries_both_the_code_and_the_label(django_user_model):
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.doctrines.services import readiness_summary_for_character
    from apps.sso.models import EveCharacter

    user = django_user_model.objects.create(username="dmap-pilot")
    character = EveCharacter.objects.create(character_id=91234567, user=user,
                                            name="D Map", is_main=True)
    doctrine = Doctrine.objects.create(name="Armour HAC", status=Doctrine.Status.ACTIVE)
    DoctrineFit.objects.create(doctrine=doctrine, name="Zealot", ship_type_id=600, eft_text="")

    with _translated_de({"unknown": "unbekannt", "optimal": "optimal"}):
        rows = readiness_summary_for_character(character)
        assert rows
        row = rows[0]
        assert row["status"] in {"optimal", "viable", "not_ready", "unknown"}
        assert str(row["status_label"]) == {"unknown": "unbekannt", "optimal": "optimal"}.get(
            row["status"], str(readiness_label(row["status"]))
        )


# =========================================================================== #
#  Pilots: contribution units
# =========================================================================== #
def test_pilot_unit_label_map_keeps_the_isk_code_intact():
    from apps.pilots.models import ContributionEvent

    ev = ContributionEvent(kind="isk_donated", magnitude=Decimal("1"), unit="isk")
    with _translated_de({"ISK": "ISK", "units": "Einheiten", "tasks": "Aufgaben"}):
        assert str(unit_label("tasks")) == "Aufgaben"
        assert str(unit_label("units")) == "Einheiten"
        # The unit stays 'isk' — the ``{% if ev.unit == 'isk' %}`` ISK formatter still fires.
        assert ev.unit == "isk"
        assert str(ev.unit_label) == "ISK"


# =========================================================================== #
#  Every template touched by this wave still compiles
# =========================================================================== #
@pytest.mark.parametrize("name", [
    "industry/chain.html",
    "industry/_chain_node.html",
    "planetary/recommend.html",
    "planetary/landing.html",
    "planetary/detail.html",
    "doctrines/readiness.html",
    "doctrines/my_readiness.html",
    "pilots/contributions.html",
    "identity/_command_deck.html",
])
def test_industry_planetary_doctrine_pilot_display_templates_compile(name):
    assert get_template(name) is not None
