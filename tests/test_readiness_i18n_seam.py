"""Seam B — a finding written by the Celery beat must render in the READER's language.

The bug class this pins: ``ReadinessFinding.title``/``detail``/``task_title``,
``ReadinessAlert.summary`` and the risk lines archived in ``ExecutiveReport.body`` are all
built as prose and WRITTEN INTO THE DATABASE by a Celery beat — which has no request and no
reader, and therefore no locale. Whatever was active at save time is frozen into the row and
served to every reader in every language, forever. ``gettext``/``gettext_lazy`` at the write
site cannot fix that (Django coerces the proxy to ``str`` on ``.save()``); it would only pick
*which* language gets frozen, sail through ``makemessages``, and translate nothing.

So these tests do the only thing that actually proves the seam works:

1. write the row through the REAL code path, under English (the worker's locale);
2. read it back under ``translation.override("de")`` with the msgstr genuinely SEEDED
   (``_translated_de``) — because the shipped ``de`` catalogue has no msgstr for these msgids
   yet, so a plain ``override("de")`` would return the English msgid and hide the bug;
3. assert the reader sees GERMAN, and that the stored English column never changed.

Plus the legacy guard: a row written before the ``*_key`` columns existed carries no key and
must still render its stored English VERBATIM — never blank.
"""
from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from django.utils import timezone, translation

from apps.readiness import alerts as alerts_module
from apps.readiness import report as report_module
from apps.readiness.engine.base import Finding
from apps.readiness.findings import upsert_findings
from apps.readiness.forecast import forecast_findings
from apps.readiness.models import ExecutiveReport, ReadinessAlert, ReadinessFinding, ReadinessSnapshot

_MISSING = object()


@contextlib.contextmanager
def _translated_de(**msgstrs):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue.

    Same helper as ``tests/test_campaigns_services.py`` — seeding the msgstr is what a
    translator filling in the .po entry does, and it is what makes a write-time-frozen row
    visibly, reproducibly broken instead of silently English.
    """
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


# The msgstrs a translator would supply. Note the reordered/rephrased German — a translation is
# not a token swap, which is exactly why the params must be interpolated at READ time.
DE_BACKLOG = "%(backlog)s SRP-Anträge offen (max. %(max)s)"
DE_CLEAR = "SRP-Rückstand abarbeiten (%(backlog)s offen)"
DE_FORECAST_TITLE = "Prognose: %(dimension)s könnte Rot in ~%(days)sd durchbrechen"
DE_FORECAST_DETAIL = "%(label)s bewegt sich in ~%(days)sd auf sein rotes Band zu"


def _srp_finding():
    """The exact Finding the SRP provider emits for a pending-claims backlog."""
    label_params = {"backlog": 7, "max": 3}
    task_params = {"backlog": 7}
    return Finding(
        kind="gap", dimension_key="srp", kpi_key="srp.pending_backlog",
        severity="high", weight=7.0,
        label="7 SRP claims pending (max 3)",
        label_key="srp.pending_backlog", label_params=label_params,
        ref_type="srp", ref_id="pending_backlog",
        task_type="other",
        task_title="Clear the SRP backlog (7 pending)",
        task_title_key="srp.clear_backlog_task", task_title_params=task_params,
    )


@pytest.mark.django_db
def test_worker_written_finding_renders_german_for_a_german_reader():
    # 1) The WRITE — the Celery beat's path, under the worker's locale (English).
    with translation.override("en"):
        upsert_findings([_srp_finding()])

    f = ReadinessFinding.objects.get(dimension_key="srp", kpi_key="srp.pending_backlog")

    # The prose column is still English — the audit record and the fallback, unchanged.
    assert f.title == "7 SRP claims pending (max 3)"
    assert f.task_title == "Clear the SRP backlog (7 pending)"
    # …and the row carries the key + PLAIN JSON params (no lazy proxy: that is a TypeError
    # inside a JSONField, and a frozen string inside a CharField).
    assert f.title_key == "srp.pending_backlog"
    assert f.title_params == {"backlog": 7, "max": 3}
    assert all(isinstance(v, int | str) for v in f.title_params.values())

    # 2) The READ — a different person, on a German request. THIS is the whole point.
    with _translated_de(**{"%(backlog)s SRP claims pending (max %(max)s)": DE_BACKLOG,
                           "Clear the SRP backlog (%(backlog)s pending)": DE_CLEAR}):
        f = ReadinessFinding.objects.get(pk=f.pk)
        assert f.title_i18n == "7 SRP-Anträge offen (max. 3)"
        assert f.task_title_i18n == "SRP-Rückstand abarbeiten (7 offen)"

    # 3) English is unchanged for an English reader, and the DB was never rewritten.
    with translation.override("en"):
        assert ReadinessFinding.objects.get(pk=f.pk).title_i18n == "7 SRP claims pending (max 3)"
    assert ReadinessFinding.objects.get(pk=f.pk).title == "7 SRP claims pending (max 3)"


@pytest.mark.django_db
def test_legacy_row_with_no_key_still_renders_its_stored_english_verbatim():
    """Nothing is backfilled. A row written before this change has no key — it must degrade to
    its stored English, never to blank and never to a wrong translation."""
    legacy = ReadinessFinding.objects.create(
        dimension_key="srp", kpi_key="srp.pending_backlog",
        ref_type="srp", ref_id="legacy",
        title="9 SRP claims pending (max 3)",
        detail="Written before the key columns existed.",
        task_title="Clear the SRP backlog (9 pending)",
    )
    assert legacy.title_key == ""
    assert legacy.title_params == {}

    # Even with the German catalogue fully seeded, a keyless row cannot and must not translate.
    with _translated_de(**{"%(backlog)s SRP claims pending (max %(max)s)": DE_BACKLOG}):
        legacy = ReadinessFinding.objects.get(pk=legacy.pk)
        assert legacy.title_i18n == "9 SRP claims pending (max 3)"
        assert legacy.detail_i18n == "Written before the key columns existed."
        assert legacy.task_title_i18n == "Clear the SRP backlog (9 pending)"
        assert legacy.title_i18n  # never blank


@pytest.mark.django_db
def test_a_german_writer_still_persists_english_prose():
    """The write site must be locale-INDEPENDENT: an officer triggering a warm from a German
    browser must not freeze German into the column every other reader falls back to."""
    with _translated_de(**{"%(backlog)s SRP claims pending (max %(max)s)": DE_BACKLOG}):
        upsert_findings([_srp_finding()])

    f = ReadinessFinding.objects.get(dimension_key="srp", kpi_key="srp.pending_backlog")
    assert f.title == "7 SRP claims pending (max 3)"  # English, not "7 SRP-Anträge offen…"


@pytest.mark.django_db
def test_alert_summary_carries_the_findings_key_and_renders_german():
    with translation.override("en"):
        upsert_findings([_srp_finding()])
        finding = ReadinessFinding.objects.get(kpi_key="srp.pending_backlog")
        alert = ReadinessAlert.objects.create(
            rule_key="srp.backlog", dimension_key="srp", kpi_key="srp.pending_backlog",
            severity="high", summary=finding.title[:300],
            summary_key=finding.title_key, summary_params=finding.title_params,
            finding=finding,
        )

    assert alert.summary == "7 SRP claims pending (max 3)"
    with _translated_de(**{"%(backlog)s SRP claims pending (max %(max)s)": DE_BACKLOG}):
        alert = ReadinessAlert.objects.get(pk=alert.pk)
        assert alert.summary_i18n == "7 SRP-Anträge offen (max. 3)"
        # The Discord/EVE-mail render also follows the reader's locale (the dispatcher enters
        # translation.override(lang) per recipient).
        message = alerts_module.render_alert(
            {"key": "srp.backlog", "severity": "high"}, alert.finding, {},
        )
        assert "7 SRP-Anträge offen (max. 3)" in message

    # A legacy alert (no key) keeps rendering its stored English.
    legacy = ReadinessAlert.objects.create(
        rule_key="srp.backlog", dimension_key="srp", kpi_key="other",
        severity="high", summary="9 SRP claims pending (max 3)",
    )
    with _translated_de(**{"%(backlog)s SRP claims pending (max %(max)s)": DE_BACKLOG}):
        assert ReadinessAlert.objects.get(pk=legacy.pk).summary_i18n == "9 SRP claims pending (max 3)"


@pytest.mark.django_db
def test_forecast_beat_writes_english_prose_plus_a_key_that_renders_german():
    """``forecast_findings`` writes ReadinessFinding.title/detail directly from the beat."""
    now = timezone.now()
    # A steadily declining doctrine score → a projected red-band breach inside the window.
    for i, score in enumerate([90, 80, 70, 60, 50, 45]):
        snap = ReadinessSnapshot.objects.create(index=score, dimensions={"doctrine": score})
        ReadinessSnapshot.objects.filter(pk=snap.pk).update(
            created_at=now - dt.timedelta(days=(5 - i) * 2)
        )

    with translation.override("en"):
        assert forecast_findings(now=now) == 1

    f = ReadinessFinding.objects.get(kind=ReadinessFinding.Kind.FORECAST)
    assert f.title.startswith("Forecast: doctrine may breach red in ~")
    assert f.title_key == "forecast.breach_title"
    assert f.detail_key == "forecast.breach_detail"
    days = f.title_params["days"]

    with _translated_de(**{"Forecast: %(dimension)s may breach red in ~%(days)sd": DE_FORECAST_TITLE,
                           "%(label)s trending toward its red band in ~%(days)sd": DE_FORECAST_DETAIL}):
        f = ReadinessFinding.objects.get(pk=f.pk)
        assert f.title_i18n == f"Prognose: doctrine könnte Rot in ~{days}d durchbrechen"
        assert "bewegt sich" in f.detail_i18n


@pytest.mark.django_db
def test_archived_report_risk_lines_render_german():
    """``ExecutiveReport.body`` is a JSONField, so the risk lines carry the key INSIDE the JSON.
    A lazy proxy in there would be a hard TypeError on save — these are plain values."""
    with translation.override("en"):
        upsert_findings([_srp_finding()])
        body = report_module.build_report(timezone.now() - dt.timedelta(days=7), timezone.now().date())
        report = ExecutiveReport.objects.create(
            period_start=timezone.now().date() - dt.timedelta(days=7),
            period_end=timezone.now().date(), index=body["index"], body=body,
        )

    risk = ExecutiveReport.objects.get(pk=report.pk).body["top_risks"][0]
    assert risk["title"] == "7 SRP claims pending (max 3)"      # English audit record
    assert risk["title_key"] == "srp.pending_backlog"

    with _translated_de(**{"%(backlog)s SRP claims pending (max %(max)s)": DE_BACKLOG}):
        assert report_module.risk_title(risk) == "7 SRP-Anträge offen (max. 3)"
        # …and the whole digest follows the reader's locale.
        digest = report_module.format_digest(
            ExecutiveReport.objects.get(pk=report.pk).body,
            timezone.now().date() - dt.timedelta(days=7), timezone.now().date(),
        )
        assert "7 SRP-Anträge offen (max. 3)" in digest

    # A report archived before this change has no key on its risks → stored English, verbatim.
    assert report_module.risk_title({"title": "9 SRP claims pending (max 3)"}) == \
        "9 SRP claims pending (max 3)"


@pytest.mark.django_db
def test_task_bridged_from_a_finding_keeps_english_but_the_finding_can_re_render():
    """``tasks.Task.title`` is written English by the beat (the tasks app owns no key column);
    the readiness lens re-renders it from the finding, which does."""
    from apps.readiness.tasks_bridge import task_for_finding

    with translation.override("en"):
        upsert_findings([_srp_finding()])
        finding = ReadinessFinding.objects.get(kpi_key="srp.pending_backlog")
        task = task_for_finding(finding)

    assert task.title == "Clear the SRP backlog (7 pending)"
    with _translated_de(**{"Clear the SRP backlog (%(backlog)s pending)": DE_CLEAR}):
        finding = ReadinessFinding.objects.get(pk=finding.pk)
        assert finding.task_title_i18n == "SRP-Rückstand abarbeiten (7 offen)"


@pytest.mark.django_db
def test_every_scaffold_key_is_resolvable_and_never_returns_blank():
    """A key that no longer exists in the registry (a rollback, a rename) must degrade to the
    stored English rather than blank the risk register."""
    from apps.readiness.messages import SCAFFOLDS, render_text

    assert SCAFFOLDS  # the registry is populated
    for key, msgid in SCAFFOLDS.items():
        assert "%(" in str(msgid) or "%" not in str(msgid), f"{key}: positional/bare % placeholder"

    assert render_text("no.such.key", {"a": 1}, "stored English") == "stored English"
    assert render_text("srp.pending_backlog", {}, "stored English") == "stored English"  # bad params
    assert render_text("", None, "stored English") == "stored English"
