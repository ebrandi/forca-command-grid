"""Command Intelligence — **Seam B**: prose written to the DB by a worker, read by other people.

Every sentence under test here is produced by a Celery job (``command_intel.generate_report``,
``command_intel.analyse_battle``, the pilot-directive refresh). A worker has no request and no user,
so it has no locale: it runs in English. Wrapping those write sites in ``gettext``/``gettext_lazy``
would be a *silent no-op* — Django coerces a lazy proxy to ``str`` on ``.save()``, so the row freezes
in the writer's (English) locale and every reader, in every locale, forever sees English. (Inside a
JSONField a proxy is not even that: it is a hard ``TypeError`` at save time.)

So the write site persists a scaffold **key + JSON-safe params** next to the English prose, and the
read site re-resolves it under the **reader's** locale. These tests pin exactly that, and they are
the whole point of the change: without them we would only have proved the columns exist.

Each test therefore:
  1. writes a row through the REAL code path, under English, as the worker does;
  2. asserts the English column is byte-for-byte what it always was (English output unchanged);
  3. reads it back under ``translation.override("de")`` with a seeded msgstr and asserts GERMAN;
  4. asserts a LEGACY row (written before this change, so no key) still renders its stored English.
"""
from __future__ import annotations

import contextlib
import json

import pytest
from django.utils import timezone, translation

from apps.command_intel import battle_analysis, messages, pilot
from apps.command_intel import report as report_mod
from apps.command_intel.models import (
    BattleAnalysis,
    CourseOfAction,
    IntelligenceReport,
    IntelligenceSnapshot,
    OperationalConstraint,
    PilotDirective,
)

_MISSING = object()


@contextlib.contextmanager
def _translated_de(**msgstrs):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue.

    The shipped ``de`` catalogue has no msgstr for these scaffolds *yet*, so a plain
    ``translation.override("de")`` would still return the English msgid and the bug would stay
    invisible. Seeding the msgstrs here is what a translator filling in that .po entry does — it
    makes the latent breakage reproducible now, and pins the invariant so it cannot regress later.

    (The ``tests/test_campaigns_services.py`` helper, reused verbatim — same catalogue mechanics.)
    """
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


# The msgids the CI worker writes, and a German msgstr for each. Keeping the msgid text here (rather
# than reaching into SCAFFOLDS) means a careless edit to a msgid fails these tests loudly, which is
# the point: an .po entry is keyed by the msgid, so changing one silently orphans its translations.
DE = {
    "%(doctrine)s fleet size": "%(doctrine)s Flottengröße",
    "%(doctrine)s stock": "%(doctrine)s Bestand",
    "Stage %(n)s more %(label)s": "Stelle %(n)s weitere %(label)s bereit",
    "%(label)s stays binding at %(metric)s %(unit)s.": (
        "%(label)s bleibt bindend bei %(metric)s %(unit)s."
    ),
    "%(label)s is binding (%(metric)s %(unit)s)": "%(label)s ist bindend (%(metric)s %(unit)s)",
    "Trend forecasting requires snapshot history (accumulates over time).": (
        "Trendprognosen erfordern eine Snapshot-Historie."
    ),
    "Command Intelligence Report — %(date)s": "Kommando-Aufklärungsbericht — %(date)s",
    "Train into %(doctrine)s": "Trainiere auf %(doctrine)s",
    "under an hour": "weniger als eine Stunde",
    "You're about %(eta)s of training from flying %(doctrine)s, one of the corp's doctrines. "
    "Closing this makes you — and the corp — more ready.": (
        "Dir fehlen etwa %(eta)s Training, um %(doctrine)s zu fliegen."
    ),
    "After-action — %(battle)s": "Nachbesprechung — %(battle)s",
    "Even": "Ausgeglichen",
    "the field": "das Schlachtfeld",
    "Deterministic facts only (AI narrative unavailable). See the panels below.": (
        "Nur deterministische Fakten (KI-Erzählung nicht verfügbar)."
    ),
}


def _summary_msgid(readiness: bool) -> str:
    key = "report.summary.degraded.readiness" if readiness else "report.summary.degraded"
    return str(messages.SCAFFOLDS[key])


def _snapshot() -> IntelligenceSnapshot:
    """A snapshot whose Ferox doctrine is short on hulls — a CRITICAL, COA-generating constraint."""
    return IntelligenceSnapshot.objects.create(slices={
        "doctrine": {"doctrines": [
            {"name": "Ferox", "slug": "ferox", "primary": True,
             "flyable": 18, "hulls_in_stock": 12, "min_pilots": 22},
        ]},
        "readiness": {"overall_index": 61},
    })


def _degraded_report() -> IntelligenceReport:
    """Run the REAL generation pipeline. The test settings carry no LLM_API_KEY, so
    COMMAND_INTEL_ENABLED is False and we take the deterministic path the worker takes when the
    AI is down — the branch that writes code-authored prose into the database."""
    _snapshot()  # resolve_snapshot() picks up the freshest snapshot
    report = IntelligenceReport.objects.create(status=IntelligenceReport.Status.QUEUED)
    report_mod.run_generation(report)
    report.refresh_from_db()
    assert report.status == IntelligenceReport.Status.READY_DEGRADED
    return report


# --------------------------------------------------------------------------- #
#  OperationalConstraint.label / .detail
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_constraint_written_in_english_renders_german_for_a_german_reader():
    report = _degraded_report()
    c = OperationalConstraint.objects.get(snapshot=report.snapshot, key="fleet_size.ferox")

    # 1) English output is unchanged — the prose column is exactly what it always was.
    assert c.label == "Ferox fleet size"
    assert c.label_i18n == "Ferox fleet size"
    assert c.detail.startswith("Max Ferox fleet = 12 pilots, limited by hulls_in_stock")

    # 2) The row carries the scaffold, and the params are plain JSON (no lazy proxy could survive
    #    a JSONField at all — that is a TypeError on save, which is why keys+params exist).
    assert c.label_key == "constraint.fleet_size.label"
    assert c.label_params == {"doctrine": "Ferox"}
    json.dumps(c.label_params)  # would raise on a gettext_lazy proxy

    # 3) The READER's locale wins, from the same stored row. This is the whole seam.
    with _translated_de(**DE):
        fresh = OperationalConstraint.objects.get(pk=c.pk)
        assert fresh.label_i18n == "Ferox Flottengröße"
        # The EVE doctrine name stays English inside the translated sentence (protected term).
        assert "Ferox" in fresh.label_i18n
        # ...and the untouched English column is still the audit record.
        assert fresh.label == "Ferox fleet size"


@pytest.mark.django_db
def test_legacy_constraint_without_a_key_renders_its_stored_english_never_blank():
    # A row written BEFORE this change: prose only, no key. Nothing is backfilled, so this is the
    # shape of every existing production row.
    snap = IntelligenceSnapshot.objects.create(slices={})
    legacy = OperationalConstraint.objects.create(
        snapshot=snap, key="fleet_size.legacy", category="combat",
        label="Legacy fleet size", detail="Legacy detail sentence.",
    )
    assert legacy.label_key == "" and legacy.label_params == {}

    with _translated_de(**DE):
        fresh = OperationalConstraint.objects.get(pk=legacy.pk)
        assert fresh.label_i18n == "Legacy fleet size"       # verbatim English, never blank
        assert fresh.detail_i18n == "Legacy detail sentence."


# --------------------------------------------------------------------------- #
#  CourseOfAction.objective / .reasoning / .risk_if_ignored
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_templated_coa_renders_german_and_its_slug_stays_english():
    _degraded_report()
    coa = CourseOfAction.objects.filter(constraint__key="fleet_size.ferox").first()
    assert coa is not None

    english_objective = coa.objective
    assert english_objective == "Stage 10 more doctrine:ferox"
    assert coa.risk_if_ignored == "Ferox fleet size stays binding at 12 pilots."

    with _translated_de(**DE):
        fresh = CourseOfAction.objects.get(pk=coa.pk)
        assert fresh.objective_i18n == "Stelle 10 weitere doctrine:ferox bereit"
        # The nested constraint label resolves under the reader's locale too — a sentence composed
        # of another scaffolded sentence, not a frozen English fragment.
        assert fresh.risk_if_ignored_i18n == "Ferox Flottengröße bleibt bindend bei 12 pilots."
        # The slug is an IDENTIFIER (the dedupe key). It must never be built from translated text.
        assert fresh.slug == coa.slug
        assert "flottengröße" not in fresh.slug.lower()
        assert fresh.objective == english_objective  # the English column is untouched


@pytest.mark.django_db
def test_llm_drafted_coa_has_no_key_and_renders_verbatim():
    # Model free text has no msgid: it can only ever be rendered verbatim (the pingboard
    # ``custom_message`` contract). It must not blank, and it must not be "translated".
    from apps.command_intel import coa as coa_mod

    snap = _snapshot()
    report = IntelligenceReport.objects.create(status=IntelligenceReport.Status.READY, snapshot=snap)
    drafts = [{
        "constraint_key": "", "objective": "Escalate to the alliance", "priority": 90,
        "reasoning": "The model's own words.", "risk_if_ignored": "The model's own risk.",
    }]
    coa_mod.persist_coas(report, drafts, {}, {})
    coa = CourseOfAction.objects.get(objective="Escalate to the alliance")
    assert coa.objective_key == "" and coa.risk_if_ignored_key == ""

    with _translated_de(**DE):
        fresh = CourseOfAction.objects.get(pk=coa.pk)
        assert fresh.objective_i18n == "Escalate to the alliance"
        assert fresh.risk_if_ignored_i18n == "The model's own risk."
        assert fresh.reasoning_i18n == "The model's own words."


# --------------------------------------------------------------------------- #
#  IntelligenceReport.title / .summary / .body  (the JSONField document)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_degraded_briefing_body_rerenders_per_reader():
    report = _degraded_report()

    # English unchanged: the stored body is byte-for-byte the sentences this pipeline always wrote.
    assert report.body["_degraded"] is True
    assert report.body["executive_summary"].endswith(
        "Narrative unavailable (AI offline) — deterministic operational picture below."
    )
    assert "Overall readiness index 61." in report.body["executive_summary"]
    assert report.body["forecast"] == (
        "Trend forecasting requires snapshot history (accumulates over time)."
    )
    assert report.summary == report.body["executive_summary"][:1000]
    assert report.title == f"Command Intelligence Report — {timezone.now():%Y-%m-%d}"

    # The document template is plain JSON — a lazy proxy here would have raised on save().
    json.dumps(report.body_params)
    assert report.body_key == "report.body.degraded"

    de = {**DE, _summary_msgid(readiness=True): (
        "%(crit)s kritische und %(high)s hohe Einschränkung(en). Bereitschaftsindex %(readiness)s."
    )}
    with _translated_de(**de):
        fresh = IntelligenceReport.objects.get(pk=report.pk)
        body = fresh.body_i18n

        # The whole document comes back in German, with the SAME shape and keys as ``body``.
        assert set(body) == set(fresh.body)
        assert body["executive_summary"] == (
            "2 kritische und 0 hohe Einschränkung(en). Bereitschaftsindex 61."
        )
        assert body["forecast"] == "Trendprognosen erfordern eine Snapshot-Historie."
        # Nested prose inside the document resolves too: each risk QUOTES its constraint's label,
        # and that embedded sentence is itself re-rendered — not a frozen English fragment.
        risks = {r["linked_constraint"]: r["risk"] for r in body["strategic_risks"]}
        assert risks["fleet_size.ferox"].startswith("Ferox Flottengröße ist bindend")
        assert risks["doctrine_stock.ferox"].startswith("Ferox Bestand ist bindend")
        # Non-prose leaves are untouched data.
        assert body["operational_picture"]["overall_readiness"] == 61

        assert fresh.summary_i18n == (
            "2 kritische und 0 hohe Einschränkung(en). Bereitschaftsindex 61."
        )
        assert fresh.title_i18n.startswith("Kommando-Aufklärungsbericht — ")
        # The English columns are untouched — still the fallback and the audit record.
        assert fresh.body["forecast"].startswith("Trend forecasting")
        assert fresh.summary == report.summary


@pytest.mark.django_db
def test_legacy_report_body_without_a_key_renders_its_stored_english():
    legacy = IntelligenceReport.objects.create(
        status=IntelligenceReport.Status.READY,
        title="Legacy briefing", summary="Legacy summary.",
        body={"executive_summary": "Legacy exec summary.", "forecast": "Legacy forecast."},
    )
    assert legacy.body_key == ""

    with _translated_de(**DE):
        fresh = IntelligenceReport.objects.get(pk=legacy.pk)
        assert fresh.body_i18n == legacy.body          # verbatim, never blank
        assert fresh.summary_i18n == "Legacy summary."
        assert fresh.title_i18n == "Legacy briefing"


# --------------------------------------------------------------------------- #
#  PilotDirective.title / .detail
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_pilot_directive_renders_in_the_members_locale(django_user_model, monkeypatch):
    from types import SimpleNamespace

    snap = IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": []}})
    monkeypatch.setattr("apps.command_intel.pilot.latest_snapshot", lambda: snap)
    monkeypatch.setattr(
        "apps.skills.services.closest_doctrines",
        lambda ch, limit=8: [{"doctrine": "Ferox", "doctrine_id": 7, "seconds": 900}],
    )
    user = django_user_model.objects.create(username="ci-i18n-pilot")

    # The refresh runs with no locale (as the worker does) and writes English.
    pilot.compute_directives(user, SimpleNamespace(character_id=4242), persist=True)
    d = PilotDirective.objects.get(user=user, slug="doctrine/7")
    assert d.title == "Train into Ferox"
    assert d.detail.startswith("You're about under an hour of training from flying Ferox,")

    with _translated_de(**DE):
        fresh = PilotDirective.objects.get(pk=d.pk)
        assert fresh.title_i18n == "Trainiere auf Ferox"
        # The ETA is prose *inside* the sentence, so it resolves under the reader's locale too.
        assert fresh.detail_i18n == (
            "Dir fehlen etwa weniger als eine Stunde Training, um Ferox zu fliegen."
        )
        # The English title is what apps/pilots/briefing.py dedupes on — it must not move.
        assert fresh.title == "Train into Ferox"


# --------------------------------------------------------------------------- #
#  BattleAnalysis.title / .body
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_degraded_battle_aar_rerenders_per_reader(settings):
    from apps.killboard.models import BattleReport

    settings.COMMAND_INTEL_ENABLED = False  # the facts-only (degraded) worker path
    now = timezone.now()
    br = BattleReport.objects.create(title="Tama brawl", system_ids=[], start_time=now, end_time=now)
    a = BattleAnalysis.objects.create(battle_report_id=br.pk, status=BattleAnalysis.Status.PENDING)
    battle_analysis.run_battle_analysis(a)
    a.refresh_from_db()
    assert a.status == BattleAnalysis.Status.READY_DEGRADED

    # English unchanged.
    assert a.title == "After-action — Tama brawl"
    assert a.body["summary"].startswith("Even engagement in the field:")
    assert a.body["what_happened"].startswith("Deterministic facts only")
    json.dumps(a.body_params)  # plain JSON — a proxy in a JSONField is a TypeError on save
    assert a.body_key == "battle.body.degraded"

    de = {**DE, str(messages.SCAFFOLDS["battle.summary.degraded"]): (
        "%(outcome)s Gefecht in %(systems)s: %(our_losses)s Schiffe verloren."
    )}
    with _translated_de(**de):
        fresh = BattleAnalysis.objects.get(pk=a.pk)
        body = fresh.body_i18n
        # Both the outcome word and the "the field" fallback are prose and resolve under ``de``;
        # the battle's own title is killboard data and stays raw inside the translated frame.
        assert body["summary"] == "Ausgeglichen Gefecht in das Schlachtfeld: 0 Schiffe verloren."
        assert body["what_happened"] == "Nur deterministische Fakten (KI-Erzählung nicht verfügbar)."
        assert fresh.title_i18n == "Nachbesprechung — Tama brawl"
        assert fresh.body["what_happened"].startswith("Deterministic facts only")


@pytest.mark.django_db
def test_llm_narrated_aar_has_no_key_and_renders_verbatim():
    a = BattleAnalysis.objects.create(
        battle_report_id=1, status=BattleAnalysis.Status.READY,
        title="After-action — Tama brawl",
        body={"summary": "The model's narrative.", "what_happened": "A roam ganked our logi."},
    )
    with _translated_de(**DE):
        fresh = BattleAnalysis.objects.get(pk=a.pk)
        assert fresh.body_i18n == a.body        # model free text: verbatim, never "translated"
        assert fresh.title_i18n == "After-action — Tama brawl"


# --------------------------------------------------------------------------- #
#  The scaffold registry itself
# --------------------------------------------------------------------------- #
def test_every_scaffold_is_a_real_msgid_with_named_placeholders():
    for key, scaffold in messages.SCAFFOLDS.items():
        text = str(scaffold)
        assert text, f"{key} resolves to an empty msgid"
        # Positional %s in a msgid is a translator footgun (it cannot be reordered); ours are named.
        assert "%s" not in text, f"{key} uses a positional placeholder"
        assert "{" not in text, f"{key} looks like an f-string/format slot, not a %(name)s msgid"


def test_render_never_blanks_and_never_raises():
    # A retired key, a mangled translation and a missing param must all degrade to the stored prose
    # rather than blank a briefing that an officer is looking at.
    assert messages.render("", {}, "stored English") == "stored English"
    assert messages.render("no.such.key", {"a": 1}, "stored English") == "stored English"
    # A msgid whose %(slot)s the params cannot satisfy falls back to the uninterpolated msgid, not
    # to a KeyError in the middle of a page render.
    assert messages.render("report.body.forecast", {"unused": 1}, "fallback")
    assert messages.render_doc(None, {"a": 1}) == {"a": 1}
