"""Seam A — the built-in template prose that is **seeded into the database**, "translate until edited".

``apps.capsuleer.templates_builtin`` and ``apps.campaigns.templates_builtin`` are shipped English
*content*: the seed migrations write them into ``CareerTemplate`` / ``CampaignTemplate`` rows, and
``instantiate_template`` copies that prose *again* onto the campaign/goal rows an officer or pilot
then edits. ``gettext_lazy`` alone cannot localise any of it — Django coerces a lazy proxy to
``str`` on ``.save()``, freezing one locale into the row forever — so the translations live in a
code-side catalogue (``templates_i18n``) keyed by the built-in's stable key, and the *render* seam
decides per row:

* the stored text still equals the shipped English → the row is still the built-in's words →
  render the **translation**;
* it differs → someone edited it → render it **verbatim**, untranslated, in every locale.

Both halves are asserted below (that is the whole point of the design), for both apps, plus the
anti-drift gate that pins every msgid to the English the seed actually writes.

``_translated_de`` seeds real msgstrs into the active catalogue — the shipped ``de`` catalogue has
no entry for this content yet, so ``translation.override("de")`` alone would hand back the English
msgid and prove nothing. (Same approach as the ``_translated_de`` helper in
``tests/test_campaigns_services.py``.)
"""
from __future__ import annotations

import contextlib

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import translation

from apps.campaigns import templates_i18n as campaign_i18n
from apps.campaigns.models import CampaignTemplate
from apps.campaigns.services import instantiate_template as instantiate_campaign
from apps.campaigns.templates_builtin import BUILTIN as CAMPAIGN_BUILTIN
from apps.capsuleer import plan as plan_mod
from apps.capsuleer import templates_i18n as career_i18n
from apps.capsuleer.models import CareerTemplate
from apps.capsuleer.templates_builtin import BUILTIN as CAREER_BUILTIN
from apps.capsuleer.templates_builtin import sync_builtin_templates
from core.i18n import config as i18n_config

from ._campaign_utils import _campaign, _director, _objective, _reference_campaign
from ._capsuleer_utils import _character, _member

pytestmark = pytest.mark.django_db

_MISSING = object()


@contextlib.contextmanager
def _translated_de(msgstrs: dict[str, str]):
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


# =========================================================================== #
#  The anti-drift gate: the catalogue IS the shipped English
# =========================================================================== #
@pytest.mark.parametrize("mod", [campaign_i18n, career_i18n], ids=["campaigns", "capsuleer"])
def test_every_msgid_is_exactly_the_english_the_seed_writes(mod):
    """A msgid that drifts from ``templates_builtin`` silently disables the seam for that field
    (the stored English would no longer match, so the row would always render verbatim). Pin it."""
    assert set(mod.BUILTIN_MSGIDS) == set(mod.BUILTIN_ENGLISH)
    with translation.override(None):  # no catalogue: str(proxy) is the raw msgid
        for key, fields in mod.BUILTIN_ENGLISH.items():
            assert set(fields) == set(mod.BUILTIN_MSGIDS[key]), key
            for field, shipped in fields.items():
                assert str(mod.BUILTIN_MSGIDS[key][field]) == shipped, (key, field)


def test_the_catalogue_covers_every_builtin():
    """Every built-in template contributes an entry — a new path/blueprint cannot ship untranslated
    prose without failing here."""
    for t in CAMPAIGN_BUILTIN:
        assert campaign_i18n.english(t["key"], "name") == t["name"]
    for t in CAREER_BUILTIN:
        assert career_i18n.english(t["key"], "name") == t["name"]
        # ``summary`` is an alias of the ``description`` column (seeded from structure.summary).
        assert career_i18n.english(t["key"], "summary") == t["structure"]["summary"]


# =========================================================================== #
#  Campaign Command: instantiated rows
# =========================================================================== #
def test_instantiation_stamps_the_builtin_provenance_key(django_user_model):
    campaign = _reference_campaign(_director(django_user_model))

    assert campaign.source_key == "armour_bs_deployment"
    assert campaign.objectives.get(sort_order=0).source_key == "armour_bs_deployment.obj.0"
    assert campaign.workstreams.get(key="doctrine").source_key == "armour_bs_deployment.ws.doctrine"
    assert campaign.milestones.get(sort_order=0).source_key == "armour_bs_deployment.ms.0"
    assert campaign.risks.filter(source_key="armour_bs_deployment.risk.0").exists()


def test_campaign_prose_translates_until_an_officer_edits_it(django_user_model):
    """BOTH halves of the design, on a real instantiated campaign."""
    campaign = _reference_campaign(_director(django_user_model))
    objective = campaign.objectives.get(sort_order=0)
    risk = campaign.risks.get(source_key="armour_bs_deployment.risk.0")
    assert objective.title == "Qualify 35 mainline doctrine pilots"  # the shipped English is stored

    officer_words = "Only the home-region pilots count for this one."
    german_description = "Piloten, die das Mainline-Fit fliegen."
    de = {
        "Establish Armour Battleship Deployment Readiness": "Einsatzbereitschaft herstellen",
        "Qualify 35 mainline doctrine pilots": "35 Mainline-Doktrinpiloten qualifizieren",
        objective.description: german_description,
        "Run dedicated logi training fleets; recruit from allied corps.":
            "Eigene Logi-Trainingsflotten fliegen; aus verbündeten Corps rekrutieren.",
        # A msgstr also exists for what the officer will type — the seam must still NOT use it.
        officer_words: "Nur die Heimatregion-Piloten zählen hier.",
    }

    with _translated_de(de):
        # --- half 1: unedited rows render the TRANSLATED built-in text ---
        assert campaign_i18n.text(objective, "title") == "35 Mainline-Doktrinpiloten qualifizieren"
        assert campaign_i18n.text(campaign, "name") == "Einsatzbereitschaft herstellen"
        assert campaign_i18n.text(risk, "mitigation") == (
            "Eigene Logi-Trainingsflotten fliegen; aus verbündeten Corps rekrutieren."
        )

        # --- half 2: an officer edits it → it is their text now → VERBATIM, never translated ---
        objective.title = officer_words
        objective.save(update_fields=["title", "updated_at"])
        objective.refresh_from_db()

        assert campaign_i18n.text(objective, "title") == officer_words
        # …and not merely because the provenance was lost: the catalogue entry is still right there.
        assert campaign_i18n.msgid(objective.source_key, "title") is not None
        # The edit test is per FIELD, not per row: the description nobody touched still translates.
        assert campaign_i18n.text(objective, "description") == german_description


def test_english_behaviour_is_unchanged(django_user_model):
    """No locale active → every row renders exactly the English stored in the database."""
    campaign = _reference_campaign(_director(django_user_model))
    for objective in campaign.objectives.all():
        assert campaign_i18n.text(objective, "title") == objective.title
        assert campaign_i18n.text(objective, "description") == objective.description
    for risk in campaign.risks.all():
        assert campaign_i18n.text(risk, "description") == risk.description


def test_corp_template_content_is_never_translated(django_user_model):
    """Corp-authored prose is user content: it has no catalogue entry and never passes through
    gettext — even when a msgstr for that exact sentence happens to exist."""
    corp_words = "Fly the new corp doctrine every Saturday."
    template = CampaignTemplate.objects.create(
        key="corp_saturday_ops", name="Saturday ops", category="training",
        blueprint={"objectives": [{"title": corp_words, "unit": "fleets", "target_value": 4}]},
    )
    campaign = instantiate_campaign(template, _director(django_user_model))
    objective = campaign.objectives.get()

    assert objective.source_key == "corp_saturday_ops.obj.0"  # provenance recorded all the same
    with _translated_de({corp_words: "Jeden Samstag die neue Corp-Doktrin fliegen."}):
        assert campaign_i18n.text(objective, "title") == corp_words


def test_a_hand_made_row_never_borrows_a_catalogue_key(django_user_model):
    """A lane's ``key`` is a campaign-local slug, not a built-in key. An empty ``source_key`` means
    "not from a built-in" and must end the lookup — never fall through to another identifier that
    happens to collide with a template key."""
    campaign = _reference_campaign(_director(django_user_model))
    lane = campaign.workstreams.create(
        key="doctrine_rollout", name="Doctrine Rollout", sort_order=99  # collides with a builtin key
    )
    assert lane.source_key == ""

    with _translated_de({"Doctrine Rollout": "Doktrin-Einführung"}):
        assert campaign_i18n.text(lane, "name") == "Doctrine Rollout"


def test_builtin_campaign_template_row_translates_until_an_operator_edits_it():
    """Part 1 — the ``CampaignTemplate`` row itself (seeded by the data migration)."""
    template = CampaignTemplate.objects.get(key="armour_bs_deployment")
    de = {"Establish Armour Battleship Deployment Readiness": "Einsatzbereitschaft herstellen"}

    with _translated_de(de):
        assert campaign_i18n.text(template, "name") == "Einsatzbereitschaft herstellen"

        template.name = "Operator-renamed"
        template.save(update_fields=["name", "updated_at"])
        assert campaign_i18n.text(template, "name") == "Operator-renamed"


# =========================================================================== #
#  Capsuleer Path: instantiated rows
# =========================================================================== #
def _mentor_goal(django_user_model):
    """A goal instantiated from the built-in ``mentor`` path (its milestones need no SDE rows)."""
    sync_builtin_templates()
    user = _member(django_user_model, "i18n")
    character = _character(user, 9101, "Seam Pilot")
    template = CareerTemplate.objects.get(key="mentor")
    return template, plan_mod.instantiate_template(template, user, character=character)


def test_career_milestone_translates_until_the_pilot_edits_it(django_user_model):
    """BOTH halves of the design, on a real instantiated goal."""
    template, goal = _mentor_goal(django_user_model)
    milestone = goal.milestones.get(order=2)
    assert milestone.source_key == "mentor.ms.2"
    assert milestone.title == "Register a mentor profile with your focus areas"

    pilot_words = "Write my mentor bio and pick two focus areas."
    de = {
        "Mentor": "Mentorin/Mentor",
        "Register a mentor profile with your focus areas":
            "Mentorprofil mit deinen Schwerpunkten anlegen",
        pilot_words: "Mentor-Bio schreiben und zwei Schwerpunkte wählen.",
    }

    with _translated_de(de):
        # --- half 1: the unedited milestone renders the TRANSLATED built-in title ---
        assert career_i18n.text(milestone, "title") == "Mentorprofil mit deinen Schwerpunkten anlegen"
        # The goal's title is the path's *name*, so it translates off the goal's template_key.
        assert career_i18n.text(goal, "title", msgid_field="name") == "Mentorin/Mentor"

        # --- half 2: the pilot rewords it → their text → VERBATIM, never translated ---
        milestone.title = pilot_words
        milestone.save(update_fields=["title", "updated_at"])
        milestone.refresh_from_db()

        assert career_i18n.text(milestone, "title") == pilot_words
        assert career_i18n.msgid(milestone.source_key, "title") is not None  # key still resolves

    # English is unchanged: with no locale active the stored English comes straight back.
    other = goal.milestones.get(order=3)
    assert career_i18n.text(other, "title") == other.title
    assert template.key == "mentor"


def test_builtin_career_template_row_and_structure_prose_translate():
    """Part 1 — the ``CareerTemplate`` row (columns) and the prose inside its ``structure`` JSON."""
    sync_builtin_templates()
    template = CareerTemplate.objects.get(key="tackle_pilot")
    de = {
        "Tackle Pilot": "Tackle-Pilot",
        "Learn to hold tackle and fly fast frigates in corp fleets.":
            "Lerne zu tacklen und schnelle Fregatten in Corp-Flotten zu fliegen.",
        "Very low — T1 frigates are cheap and expected to die.":
            "Sehr gering — T1-Fregatten sind billig und sollen sterben.",
        "Overview setup": "Overview-Einrichtung",
        "Costs exclude implants.": "Kosten ohne Implantate.",
    }

    with _translated_de(de):
        assert career_i18n.text(template, "name") == "Tackle-Pilot"
        assert career_i18n.text(template, "description") == (
            "Lerne zu tacklen und schnelle Fregatten in Corp-Flotten zu fliegen."
        )
        assert career_i18n.text(template, "cost_note") == (
            "Sehr gering — T1-Fregatten sind billig und sollen sterben."
        )
        # Prose that lives inside the structure JSON is addressed by the same seam.
        structure = template.structure
        kb = structure["knowledge_links"][0]
        assert career_i18n.render(
            career_i18n.knowledge_key(template.key, kb["kb_slug"]), "label", kb["label"]
        ) == "Overview-Einrichtung"
        assumption = structure["assumptions"][1]
        assert career_i18n.render(
            career_i18n.assumption_key(template.key, 1), "text", assumption
        ) == "Kosten ohne Implantate."


# =========================================================================== #
#  The truncation trap (a copied column is truncated; the comparison must be too)
# =========================================================================== #
def test_render_compares_against_the_truncated_english(monkeypatch):
    """``instantiate_template`` truncates as it copies (``title[:200]``). If the seam compared the
    stored value against the *untruncated* English, a long built-in string would look permanently
    "edited" and would never translate."""
    key = "unit_test.obj.0"
    long_english = "Stage " + "very " * 60 + "many hulls."
    monkeypatch.setitem(campaign_i18n.BUILTIN_ENGLISH, key, {"title": long_english})
    monkeypatch.setitem(campaign_i18n.BUILTIN_MSGIDS, key, {"title": "Viele Hüllen bereitstellen."})

    stored = long_english[:200]  # what the Objective.title column would actually hold
    assert campaign_i18n.render(key, "title", stored, max_length=200) == "Viele Hüllen bereitstellen."
    # Same row, but told nothing about the column's limit: it cannot tell truncation from an edit.
    assert campaign_i18n.render(key, "title", stored) == stored


# =========================================================================== #
#  The PAGES: the templates must actually go through the seam
# =========================================================================== #
#  Everything above tests the seam itself. These render the real pages, because a seam nothing calls
#  translates nothing: they fail the moment a template goes back to ``{{ o.title }}`` (the raw
#  column) instead of ``{{ o.title_i18n }}`` (the model property that delegates to the seam).
#
#  The locale is driven the way a pilot's browser drives it — the language cookie through the real
#  ``core.i18n.LocaleMiddleware`` — because a bare ``translation.override`` is re-activated per
#  request by that middleware and would leave the page in English.
def _german_client(client, user):
    i18n_config.set_i18n_config(locales={"de": True})  # the progressive-reveal gate: de must be on
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "de"
    client.force_login(user)
    return client


def test_campaign_pages_render_the_builtin_translated_and_the_officer_edit_verbatim(
    client, django_user_model
):
    """Campaign detail + officer workspace, in German, on a real instantiated reference campaign."""
    director = _director(django_user_model)
    campaign = _reference_campaign(director)
    edited = campaign.objectives.get(sort_order=1)  # "Qualify 12 logistics pilots"

    officer_words = "Only the home-region pilots count for this one."
    de = {
        "Establish Armour Battleship Deployment Readiness": "Einsatzbereitschaft herstellen",
        "Qualify 35 mainline doctrine pilots": "35 Mainline-Doktrinpiloten qualifizieren",
        "Qualify 12 logistics pilots": "12 Logistik-Piloten qualifizieren",
        "First training fleet complete": "Erste Trainingsflotte abgeschlossen",
        "Insufficient logistics pilots qualified in time":
            "Nicht genug Logistik-Piloten rechtzeitig qualifiziert",
        # A msgstr exists for what the officer typed, too — the seam must still never use it.
        officer_words: "Nur die Heimatregion-Piloten zählen hier.",
    }
    edited.title = officer_words
    edited.save(update_fields=["title", "updated_at"])

    _german_client(client, director)
    with _translated_de(de):
        detail = client.get(reverse("campaigns:detail", args=[campaign.pk])).content.decode()
        workspace = client.get(reverse("campaigns:workspace")).content.decode()

    for html in (detail, workspace):
        # the untouched built-in objective renders TRANSLATED…
        assert "35 Mainline-Doktrinpiloten qualifizieren" in html
        # …and the one the officer rewrote renders in the OFFICER'S words, never translated
        assert officer_words in html
        assert "Nur die Heimatregion-Piloten zählen hier." not in html
        assert "12 Logistik-Piloten qualifizieren" not in html  # not its old built-in translation

    # The campaign, milestone and risk prose on the page go through the same seam.
    assert "Einsatzbereitschaft herstellen" in detail
    assert "Erste Trainingsflotte abgeschlossen" in detail
    assert "Nicht genug Logistik-Piloten rechtzeitig qualifiziert" in detail


def test_a_campaign_written_from_scratch_is_never_translated_on_the_page(client, django_user_model):
    """Officer-authored content, end to end: no ``source_key`` → their words, in every locale — even
    though this campaign happens to be named after a built-in whose msgstr is right there."""
    director = _director(django_user_model)
    campaign = _campaign(name="Doctrine Rollout", commander=director)
    objective = _objective(campaign, title="Qualify 20 pilots")
    assert campaign.source_key == "" and objective.source_key == ""

    de = {"Doctrine Rollout": "Doktrin-Einführung", "Qualify 20 pilots": "20 Piloten qualifizieren"}
    _german_client(client, director)
    with _translated_de(de):
        html = client.get(reverse("campaigns:detail", args=[campaign.pk])).content.decode()

    assert "Doctrine Rollout" in html and "Doktrin-Einführung" not in html
    assert "Qualify 20 pilots" in html and "20 Piloten qualifizieren" not in html


def test_goal_page_renders_the_builtin_translated_and_the_pilot_edit_verbatim(
    client, django_user_model
):
    """The same proof for Capsuleer Path: the goal page, in German, on a real instantiated goal."""
    _template, goal = _mentor_goal(django_user_model)
    edited = goal.milestones.get(order=3)  # "Take your first mentee pairing"

    pilot_words = "Pair up with a new pilot from the last intake."
    de = {
        "Mentor": "Mentorin/Mentor",  # the goal's title IS the path's name (msgid_field="name")
        "Register a mentor profile with your focus areas":
            "Mentorprofil mit deinen Schwerpunkten anlegen",
        "Take your first mentee pairing": "Nimm dein erstes Mentee-Paar an",
        pilot_words: "Schließe dich einem neuen Piloten aus der letzten Aufnahme an.",
    }
    edited.title = pilot_words
    edited.save(update_fields=["title", "updated_at"])

    _german_client(client, goal.user)
    with _translated_de(de):
        html = client.get(reverse("capsuleer:goal_detail", args=[goal.pk])).content.decode()

    assert "Mentorprofil mit deinen Schwerpunkten anlegen" in html  # unedited → translated
    assert pilot_words in html                                      # edited → the pilot's words
    assert "Schließe dich einem neuen Piloten aus der letzten Aufnahme an." not in html
    assert "Nimm dein erstes Mentee-Paar an" not in html
    assert "Mentorin/Mentor" in html                                # the goal title, off template_key
