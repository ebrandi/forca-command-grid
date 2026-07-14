"""Seam B for ``CandidateEvidence.claim``: written by a locale-less worker, read by a recruiter.

The evidence claim is prose BUILT BY ONE ACTOR AND READ BY ANOTHER. The writer is the
``recruitment.refresh_evidence`` Celery task: no request, no user, no locale — so it always runs
under English. Wrapping the sentence in ``gettext``/``gettext_lazy`` at the write site cannot fix
that: Django coerces the proxy to ``str`` on ``.save()``, freezing the row in the *writer's*
locale forever. A German recruiter would still read English, and ``makemessages`` would happily
report the string as translated. That is the trap these tests exist to catch.

So the fix is structural: persist a scaffold KEY + plain-JSON PARAMS beside the English prose, and
resolve the msgid under the READER's locale at render time. The load-bearing assertions:

* a row written under English (as the worker writes it) renders GERMAN to a German reader;
* the persisted ``claim`` column stays English regardless of the writer's locale (audit + fallback);
* a LEGACY row — no key, written before this landed — still renders its stored English verbatim,
  never blank.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest
from django.utils import translation

from apps.recruitment.models import Candidate, CandidateEvidence
from apps.recruitment.services import (
    build_esi_evidence,
    build_public_evidence,
    store_esi_evidence,
)

NOW = datetime(2026, 6, 22, tzinfo=UTC)
_MISSING = object()


@contextlib.contextmanager
def _translated_de(**msgstrs):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue.

    Mirrors the helper in ``tests/test_campaigns_services.py``. The shipped ``de`` catalogue has no
    msgstr for these scaffolds *yet*, so a plain ``translation.override("de")`` would return the
    English msgid and the bug would stay invisible. Seeding the msgstrs is exactly what a
    translator filling in that .po entry does: it makes the latent breakage reproducible now and
    pins the invariant so it cannot regress later.
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


# The msgids exactly as they appear in apps/recruitment/messages.py.
_DE = {
    "Character age: %(years)s years": "Charakteralter: %(years)s Jahre",
    "%(count)s corporation(s) in the last 12 months — worth asking about":
        "%(count)s Corporation(s) in den letzten 12 Monaten — nachfragenswert",
    "Holds roles in their current corp: %(roles)s":
        "Hat Rollen in der aktuellen Corp: %(roles)s",
    "No special roles in their current corp (line member)":
        "Keine besonderen Rollen in der aktuellen Corp (einfaches Mitglied)",
}


@pytest.fixture
def candidate(db):
    return Candidate.objects.create(character_id=99001, name="Test Pilot")


def _write_as_worker(candidate, rows):
    """Persist ``rows`` the way the Celery worker does: no request, English active."""
    with translation.override("en"):
        for r in rows:
            CandidateEvidence.objects.create(candidate=candidate, **r)


# --------------------------------------------------------------------------- #
#  The seam itself
# --------------------------------------------------------------------------- #
def test_worker_written_claim_renders_german_for_a_german_reader(candidate):
    """THE point of the whole change: English writer, German reader, German output.

    Pre-fix this was impossible — the row held one frozen English string and every reader,
    in every locale, got it.
    """
    rows = build_public_evidence({"birthday": "2022-06-22T00:00:00Z"}, [], NOW)
    _write_as_worker(candidate, rows)

    ev = CandidateEvidence.objects.get(candidate=candidate, claim_key="public.character_age")
    # The worker stored English prose + the structural key/params — never a translated sentence.
    assert ev.claim == "Character age: 4.0 years"
    assert ev.claim_params == {"years": 4.0}

    with _translated_de(**_DE):
        assert ev.claim_i18n == "Charakteralter: 4.0 Jahre"

    # …and the audit column on disk is untouched by that render.
    ev.refresh_from_db()
    assert ev.claim == "Character age: 4.0 years"


def test_interpolated_params_are_not_translated(candidate):
    """The i18n boundary sits between the sentence and its values.

    ``%(roles)s`` carries EVE corp-role names (game data, protected): the sentence around them is
    German, the role names stay canonical English.
    """
    rows = build_esi_evidence(None, {"roles": ["Director", "Station_Manager"]})
    _write_as_worker(candidate, rows)
    ev = CandidateEvidence.objects.get(candidate=candidate, theme="roles")
    assert ev.is_flag is True  # Director → flagged; the comparison never went through gettext

    with _translated_de(**{
        "Holds roles in their current corp: %(roles)s — currently a Director; "
        "confirm why they are leaving":
            "Hat Rollen in der aktuellen Corp: %(roles)s — derzeit Director; "
            "Grund für den Wechsel klären",
    }):
        out = ev.claim_i18n
    assert out.startswith("Hat Rollen in der aktuellen Corp:")
    assert "Director, Station Manager" in out  # game data, verbatim English


def test_flagged_and_unflagged_churn_are_separate_msgids(candidate):
    """The flagged variant is one whole sentence, not a plain msgid plus a translated suffix."""
    history = [{"corporation_id": i, "start_date": "2026-01-01T00:00:00Z"} for i in range(6)]
    rows = build_public_evidence({}, history, NOW)
    _write_as_worker(candidate, rows)

    ev = CandidateEvidence.objects.get(candidate=candidate, theme="risk")
    assert ev.claim_key == "risk.corp_churn_flagged"
    assert ev.claim == "6 corporation(s) in the last 12 months — worth asking about"
    with _translated_de(**_DE):
        assert ev.claim_i18n == "6 Corporation(s) in den letzten 12 Monaten — nachfragenswert"


# --------------------------------------------------------------------------- #
#  Legacy rows: nothing is backfilled, so a keyless row must degrade to English
# --------------------------------------------------------------------------- #
def test_legacy_row_without_a_key_renders_its_stored_english_verbatim(candidate):
    """A row written before this change has no key. It must render its prose — never blank."""
    legacy = CandidateEvidence.objects.create(
        candidate=candidate, theme="identity",
        claim="Character age: 7.3 years", confidence="high", source="public",
    )
    assert legacy.claim_key == "" and legacy.claim_params == {}

    with _translated_de(**_DE):
        assert legacy.claim_i18n == "Character age: 7.3 years"  # stored English, verbatim
    assert legacy.claim_i18n == "Character age: 7.3 years"


def test_unknown_key_from_another_deploy_degrades_to_english(candidate):
    """A key this deploy has never heard of falls back to the prose, rather than blanking."""
    ev = CandidateEvidence.objects.create(
        candidate=candidate, theme="identity", claim="Something from the future",
        claim_key="public.not_in_this_deploy", claim_params={"x": 1},
        confidence="high", source="public",
    )
    with _translated_de(**_DE):
        assert ev.claim_i18n == "Something from the future"


def test_claim_i18n_never_blanks_across_every_scaffold(candidate):
    """Belt and braces: every row the real code paths produce renders non-empty in de."""
    rows = build_public_evidence(
        {"birthday": "2019-01-01T00:00:00Z", "security_status": 2.54},
        [{"corporation_id": 1, "start_date": "2026-01-01T00:00:00Z"}],
        NOW, red_entities={1},
    )
    rows += build_esi_evidence(
        {"total_sp": 91_000_000, "skills": [{"trained_skill_level": 5}]},
        {"roles": []},
    )
    _write_as_worker(candidate, rows)

    with _translated_de(**_DE):
        for ev in CandidateEvidence.objects.filter(candidate=candidate):
            assert ev.claim_i18n.strip(), f"{ev.claim_key} rendered blank"


# --------------------------------------------------------------------------- #
#  English behaviour is unchanged
# --------------------------------------------------------------------------- #
def test_english_prose_is_byte_identical_to_the_pre_change_output():
    """The stored ``claim`` strings are exactly what the old f-strings produced."""
    rows = build_public_evidence(
        {"birthday": "2022-06-22T00:00:00Z", "security_status": -1.24},
        [{"corporation_id": 7, "start_date": "2026-01-01T00:00:00Z"}],
        NOW, red_entities={7},
    )
    claims = [r["claim"] for r in rows]
    assert claims == [
        "Character age: 4.0 years",
        "Security status: -1.2",
        "1 corporation(s) in the last 12 months",
        "Flew with 1 corp(s) we hold red — verify before accepting",
    ]

    esi = build_esi_evidence(
        {"total_sp": 91_000_000, "skills": [{"trained_skill_level": 5}, {"trained_skill_level": 3}]},
        {"roles": ["Director"]},
    )
    assert [r["claim"] for r in esi] == [
        "Total skill points: 91.0M (ESI-confirmed)",
        "2 skills trained, 1 at level V",
        "Holds roles in their current corp: Director — currently a Director; "
        "confirm why they are leaving",
    ]


def test_prose_column_stays_english_even_when_the_writer_is_german(candidate):
    """A write that happens on a German recruiter's request (the consent callback) must still
    persist ENGLISH prose — the column is the locale-independent audit record and fallback."""
    rows = build_esi_evidence(None, {"roles": []})
    with _translated_de(**_DE):
        store_esi_evidence(candidate, rows)

    ev = CandidateEvidence.objects.get(candidate=candidate, source="esi")
    assert ev.claim == "No special roles in their current corp (line member)"
    assert ev.claim_key == "roles.none"
    # …but that same row renders German to a German reader.
    with _translated_de(**_DE):
        assert ev.claim_i18n == "Keine besonderen Rollen in der aktuellen Corp (einfaches Mitglied)"


def test_params_are_plain_json_safe_values(candidate):
    """A ``gettext_lazy`` proxy inside a JSONField is a hard TypeError at save time. Guard it."""
    rows = build_public_evidence(
        {"birthday": "2022-06-22T00:00:00Z", "security_status": 1.0}, [], NOW
    )
    rows += build_esi_evidence({"total_sp": 5_000_000, "skills": []}, {"roles": ["Director"]})
    for r in rows:
        for v in r["claim_params"].values():
            assert type(v) in (int, float, str), f"{v!r} is not JSON-safe"
