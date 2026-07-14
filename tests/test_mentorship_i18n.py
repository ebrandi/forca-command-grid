"""Seam B for the Mentorship Program: prose that is WRITTEN TO THE DB and read back by others.

Every string covered here is produced by one actor — usually a Celery worker (``scan_anomalies``,
``sweep_api_validations``, ``auto_suggest_pairings``, ``expire_stale_pairings``,
``refresh_eligibility``), which has no user and therefore no locale — persisted, and then displayed
to *different* people under *their* locales.

That makes ``gettext`` at the write site worse than useless: Django coerces a lazy proxy to ``str``
on ``.save()``, so a naive ``_()`` would pass ``makemessages`` and freeze the row in the writer's
(English) locale forever, translating nothing. The fix is to persist a scaffold key + JSON-safe
params alongside the English prose and re-resolve it per reader.

The assertion that matters in every test below is the pair:

  * the row is written under **English** (as the worker writes it) and the stored prose stays
    English — the audit record and the fallback are untouched; and
  * the same row, read under ``de`` with the msgstr a translator would supply, renders **German**.

A test that only checked the columns exist would pass just as happily with a naive ``_()`` at the
write site, which is precisely the bug. Legacy rows (written before this change, so no key) must
still render their stored English verbatim — never blank.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django.utils.translation import override

from apps.identity.models import RoleAssignment
from apps.mentorship import eligibility, services, tasks, trust, workflow
from apps.mentorship import messages as msg
from apps.mentorship.models import (
    MenteeProfile,
    MentorProfile,
    MentorshipFlag,
    MentorshipPairing,
    MentorshipPairingEvent,
    MentorshipTaskAssignment,
    MentorshipTaskValidation,
    MentorshipTrack,
)
from apps.pilots.models import ContributionEvent
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from tests.test_campaigns_services import _translated_de


@pytest.fixture(autouse=True)
def _no_esi(monkeypatch):
    monkeypatch.setattr(
        "apps.mentorship.eligibility._fetch_facts",
        lambda ch: {"age_days": 400, "tenure_days": 200, "confidence": "high", "source": "esi"},
    )


def _member(dum, suffix, cid):
    user, _ = dum.objects.get_or_create(username=f"i18n-{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.get_or_create(
        character_id=cid,
        defaults={"user": user, "name": suffix, "is_main": True, "is_corp_member": True},
    )
    return user


def _officer(dum):
    user, _ = dum.objects.get_or_create(username="i18n-officer")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _pair(dum, **mentor_kw):
    mentor, _ = MentorProfile.objects.get_or_create(
        user=_member(dum, "mtr", 910001),
        defaults={"status": MentorProfile.Status.ACTIVE, **mentor_kw})
    mentee, _ = MenteeProfile.objects.get_or_create(
        user=_member(dum, "cdt", 910002), defaults={"status": MenteeProfile.Status.ACTIVE})
    return mentor, mentee


def _active_pairing(dum):
    mentor, mentee = _pair(dum)
    pairing = services.propose_pairing(
        mentor, mentee, initiated_by=MentorshipPairing.InitiatedBy.LEADER,
        status=MentorshipPairing.Status.PENDING_APPROVAL)
    services.approve_pairing(pairing, _officer(dum))
    pairing.refresh_from_db()
    return pairing


# ---------------------------------------------------------------------------
# MentorshipPairingEvent.detail
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_pairing_event_written_in_english_renders_german_for_a_german_reader(django_user_model):
    mentor, mentee = _pair(django_user_model)
    pairing = services.propose_pairing(
        mentor, mentee, initiated_by=MentorshipPairing.InitiatedBy.LEADER)

    event = pairing.events.get()
    # Written as the worker/leader wrote it: the English audit record is unchanged…
    assert event.detail == "Proposed (Leadership)."
    assert event.detail_key == "pairing.proposed.leader"

    # …and the German mentee reading the very same row gets German.
    with _translated_de(**{"Proposed (Leadership).": "Vorgeschlagen (Leitung)."}):
        assert event.detail_i18n == "Vorgeschlagen (Leitung)."
    event.refresh_from_db()
    assert event.detail == "Proposed (Leadership)."  # the stored row never mutated


@pytest.mark.django_db
def test_the_ttl_worker_writes_a_key_a_german_reader_can_resolve(django_user_model):
    """``mentorship.expire_stale_pairings`` runs in Celery: no request, no user, no locale."""
    mentor, mentee = _pair(django_user_model)
    pairing = services.propose_pairing(
        mentor, mentee, initiated_by=MentorshipPairing.InitiatedBy.SYSTEM)
    program = services.active_program()
    program.pairing_ttl_days = 1
    program.save()
    MentorshipPairing.objects.filter(pk=pairing.pk).update(
        created_at=timezone.now() - timedelta(days=30))

    assert tasks.expire_stale_pairings()["expired"] == 1

    event = pairing.events.filter(detail_key="pairing.auto_expired").get()
    assert event.detail == "Auto-expired (TTL)."
    with _translated_de(**{"Auto-expired (TTL).": "Automatisch abgelaufen (TTL)."}):
        assert event.detail_i18n == "Automatisch abgelaufen (TTL)."


@pytest.mark.django_db
def test_a_track_title_inside_a_translated_sentence_stays_english(django_user_model):
    """The sentence is translatable chrome; the corp-authored track title is interpolated raw.

    EVE game data and corp-authored content (a track title) stay English by policy — but the
    sentence *containing* one is still translated. That is the whole point of a param slot.
    """
    pairing = _active_pairing(django_user_model)
    # An officer enrols the pair in an optional (non-core) track — the logged path.
    track = MentorshipTrack.objects.filter(active=True, is_core=False).first()
    services.enroll_track(pairing, track, actor=_officer(django_user_model))

    event = pairing.events.get(detail_key="pairing.track_enrolled")
    assert event.detail == f"Enrolled in track: {track.title}"
    assert event.detail_params == {"track": track.title}

    with _translated_de(**{"Enrolled in track: %(track)s": "In Kurs eingeschrieben: %(track)s"}):
        # German sentence, English track title.
        assert event.detail_i18n == f"In Kurs eingeschrieben: {track.title}"


# ---------------------------------------------------------------------------
# MentorshipTaskValidation.detail + MentorshipTaskAssignment.last_reason
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_auto_check_written_by_the_sweep_worker_renders_german(django_user_model):
    """``mentorship.sweep_api_validations`` writes both sinks; the mentee reads them in German."""
    pairing = _active_pairing(django_user_model)
    assignment = pairing.assignments.get(task__key="fleet-join")  # auto_internal / fleet_attended

    # Submitted with no fleet on the ledger yet → parked PENDING_API by the request…
    workflow.mentee_submit(assignment, pairing.mentee.user)
    assignment.refresh_from_db()
    assert assignment.status == MentorshipTaskAssignment.Status.PENDING_API

    # …then the corp's fleet lands and the *worker* sweeps it up, with no locale of its own.
    ContributionEvent.objects.create(
        user=pairing.mentee.user, kind=ContributionEvent.Kind.FLEET, occurred_at=timezone.now())
    assert tasks.sweep_api_validations()["completed"] == 1

    assignment.refresh_from_db()
    validation = assignment.validations.filter(detail_key="check.fleet_attended").latest("created_at")
    # English audit record, exactly as before this change.
    assert assignment.last_reason == "1 fleet(s) attended (need 1)."
    assert validation.detail == "1 fleet(s) attended (need 1)."
    assert assignment.last_reason_params == {"count": 1, "need": 1}

    with _translated_de(**{
        "%(count)s fleet(s) attended (need %(need)s).": "%(count)s Flotte(n) geflogen (nötig: %(need)s).",
    }):
        assert assignment.last_reason_i18n == "1 Flotte(n) geflogen (nötig: 1)."
        assert validation.detail_i18n == "1 Flotte(n) geflogen (nötig: 1)."


@pytest.mark.django_db
def test_mentor_signoff_without_a_note_is_translatable_and_with_one_is_verbatim(django_user_model):
    pairing = _active_pairing(django_user_model)

    # No note → the sentence is ours, so it carries a key and localises.
    a1 = pairing.assignments.get(task__key="client-review")  # manual_mentor
    workflow.start_task(a1, pairing.mentee.user)
    workflow.mentor_decide(a1, pairing.mentor.user, approve=True)
    a1.refresh_from_db()
    assert a1.last_reason == "Mentor confirmed."
    with _translated_de(**{"Mentor confirmed.": "Vom Mentor bestätigt."}):
        assert a1.last_reason_i18n == "Vom Mentor bestätigt."

    # A note the mentor typed → their own words, stored and shown verbatim in every locale. It is
    # never machine-translated, and it never acquires a key.
    a2 = pairing.assignments.get(task__key="welcome-services")
    workflow.start_task(a2, pairing.mentee.user)
    workflow.mentor_decide(a2, pairing.mentor.user, approve=True, reason="Nice work on the fit.")
    a2.refresh_from_db()
    assert a2.last_reason_key == ""
    with _translated_de(**{"Mentor confirmed.": "Vom Mentor bestätigt."}):
        assert a2.last_reason_i18n == "Nice work on the fit."


# ---------------------------------------------------------------------------
# MentorshipFlag.detail
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_flag_raised_by_the_anomaly_worker_renders_german_for_the_officer(django_user_model):
    pairing = _active_pairing(django_user_model)
    program = services.active_program()
    program.stale_pair_days = 7
    program.save()
    MentorshipPairing.objects.filter(pk=pairing.pk).update(
        last_activity_at=timezone.now() - timedelta(days=30))
    pairing.refresh_from_db()

    assert tasks.scan_anomalies()["flags_raised"] >= 1

    flag = MentorshipFlag.objects.get(kind=MentorshipFlag.Kind.STALE_PAIR)
    assert flag.detail == "No activity for 7+ days."
    assert flag.detail_key == "flag.stale_pair" and flag.detail_params == {"days": 7}
    # The dedupe key is an identifier used in a uniqueness lookup — never prose, never translated.
    assert flag.dedupe_key == f"stale:{pairing.pk}"

    with _translated_de(**{"No activity for %(days)s+ days.": "Keine Aktivität seit %(days)s+ Tagen."}):
        assert flag.detail_i18n == "Keine Aktivität seit 7+ Tagen."
        # Re-scanning under a German reader must not rewrite the row into German.
        trust.scan_pairing(pairing)
    flag.refresh_from_db()
    assert flag.detail == "No activity for 7+ days."


# ---------------------------------------------------------------------------
# MentorshipPairing.match_reasons (JSON list)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_match_reasons_persisted_by_the_auto_suggest_worker_render_german(django_user_model):
    mentor, mentee = _pair(django_user_model, areas=["pvp", "fleet"])
    mentor.areas = ["pvp", "fleet"]
    mentor.save()
    mentee.goals = ["pvp"]
    mentee.save()

    assert tasks.auto_suggest_pairings()["suggested"] == 1
    pairing = MentorshipPairing.objects.get(mentor=mentor, mentee=mentee)

    # A lazy proxy in a JSONField is a TypeError at save time — these must be plain values.
    assert pairing.match_reasons == ["Shared focus: pvp.", *pairing.match_reasons[1:]]
    assert all(isinstance(r, str) for r in pairing.match_reasons)
    assert pairing.match_reasons_keys[0] == {"key": "match.shared_focus",
                                            "params": {"areas": "pvp"}}

    with _translated_de(**{"Shared focus: %(areas)s.": "Gemeinsamer Fokus: %(areas)s."}):
        # The pilot-entered focus area stays raw inside the translated sentence.
        assert pairing.match_reasons_i18n[0] == "Gemeinsamer Fokus: pvp."
    assert pairing.match_reasons[0] == "Shared focus: pvp."  # English column untouched


# ---------------------------------------------------------------------------
# eligibility["reasons"] (JSON list)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_eligibility_refreshed_by_the_worker_renders_german(django_user_model):
    mentor, _mentee = _pair(django_user_model)
    assert tasks.refresh_eligibility()["profiles_refreshed"] >= 1
    mentor.refresh_from_db()

    snapshot = mentor.eligibility
    assert snapshot["reasons"][0] == "Character is 1y 35d old (meets the 365d minimum)."
    assert snapshot["reasons_i18n"][0]["key"] == "elig.mentor_age_meets"

    with _translated_de(**{
        "Character is %(years)sy %(days)sd old (meets the %(min)sd minimum).":
            "Charakter ist %(years)sJ %(days)sT alt (erfüllt das Minimum von %(min)sT).",
    }):
        assert eligibility.reasons_for(snapshot)[0] == (
            "Charakter ist 1J 35T alt (erfüllt das Minimum von 365T)."
        )
        # ``for_display`` is what the view hands the template: same list, reader's locale.
        assert eligibility.for_display(snapshot)["reasons"][0].startswith("Charakter ist")
    assert mentor.eligibility["reasons"][0].startswith("Character is")


# ---------------------------------------------------------------------------
# The trap: a non-English WRITER must not poison the row
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_german_writer_still_stores_english_prose(django_user_model):
    """The bug a naive ``_()`` at the write site would cause, pinned so it cannot come back.

    A German officer approving a pairing must not freeze German into the shared audit row that the
    English mentor reads. The row stores the key; the prose column stays English.
    """
    mentor, mentee = _pair(django_user_model)
    pairing = services.propose_pairing(
        mentor, mentee, initiated_by=MentorshipPairing.InitiatedBy.LEADER,
        status=MentorshipPairing.Status.PENDING_APPROVAL)

    with _translated_de(**{"Approved by leadership.": "Von der Leitung genehmigt."}):
        services.approve_pairing(pairing, _officer(django_user_model))

    event = MentorshipPairingEvent.objects.get(detail_key="pairing.approved_by_leadership")
    assert event.detail == "Approved by leadership."   # NOT "Von der Leitung genehmigt."
    assert event.detail_params == {}                   # plain JSON, never a lazy proxy

    with override("en"):
        assert event.detail_i18n == "Approved by leadership."
    with _translated_de(**{"Approved by leadership.": "Von der Leitung genehmigt."}):
        assert event.detail_i18n == "Von der Leitung genehmigt."


# ---------------------------------------------------------------------------
# Legacy rows: no key → stored English, verbatim, never blank
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_legacy_rows_with_no_key_render_their_stored_english_verbatim(django_user_model):
    """Nothing is backfilled, so every pre-existing row has an empty key. It must still render."""
    pairing = _active_pairing(django_user_model)
    assignment = pairing.assignments.first()

    legacy_event = MentorshipPairingEvent.objects.create(
        pairing=pairing, kind=MentorshipPairingEvent.Kind.SYSTEM, detail="Proposed (Mentor).")
    legacy_flag = MentorshipFlag.objects.create(
        kind=MentorshipFlag.Kind.STALE_PAIR, pairing=pairing, detail="No activity for 7+ days.")
    legacy_validation = MentorshipTaskValidation.objects.create(
        assignment=assignment, source=MentorshipTaskValidation.Source.MENTOR,
        result=MentorshipTaskValidation.Result.PASS, detail="Mentor confirmed.")
    MentorshipTaskAssignment.objects.filter(pk=assignment.pk).update(
        last_reason="Mentor confirmed.")
    assignment.refresh_from_db()
    MentorshipPairing.objects.filter(pk=pairing.pk).update(
        match_reasons=["Shared focus: pvp."], match_reasons_keys=[])
    pairing.refresh_from_db()

    assert legacy_event.detail_key == ""  # the db_default, never backfilled

    # Even with a German catalogue loaded, a keyless row degrades to its stored English — and to
    # its stored English *only*. It must never come back blank.
    with _translated_de(**{
        "Proposed (Mentor).": "Vorgeschlagen (Mentor).",
        "No activity for %(days)s+ days.": "Keine Aktivität seit %(days)s+ Tagen.",
        "Mentor confirmed.": "Vom Mentor bestätigt.",
        "Shared focus: %(areas)s.": "Gemeinsamer Fokus: %(areas)s.",
    }):
        assert legacy_event.detail_i18n == "Proposed (Mentor)."
        assert legacy_flag.detail_i18n == "No activity for 7+ days."
        assert legacy_validation.detail_i18n == "Mentor confirmed."
        assert assignment.last_reason_i18n == "Mentor confirmed."
        assert pairing.match_reasons_i18n == ["Shared focus: pvp."]
        assert eligibility.reasons_for({"reasons": ["Corp tenure unknown."]}) == [
            "Corp tenure unknown."
        ]


@pytest.mark.django_db
def test_free_text_a_pilot_typed_is_never_translated(django_user_model):
    """A pause reason is the pilot's own words: verbatim for every reader, in every locale."""
    pairing = _active_pairing(django_user_model)
    services.pause_pairing(pairing, pairing.mentor.user, reason="Deployment, back in a week.")

    event = pairing.events.filter(to_status=MentorshipPairing.Status.PAUSED).get()
    assert event.detail == "Deployment, back in a week."
    assert event.detail_key == ""
    with _translated_de(**{"Mentor confirmed.": "Vom Mentor bestätigt."}):
        assert event.detail_i18n == "Deployment, back in a week."


def test_every_scaffold_key_is_an_identifier_and_every_msgid_is_a_literal():
    """A key is compared and persisted, so it is never prose; a msgid must be xgettext-visible."""
    for key, scaffold in msg.SCAFFOLDS.items():
        assert key == key.lower() and " " not in key
        # ``_(f"…")``/``_(variable)`` would be a silent no-op: a real proxy resolves to its msgid
        # with translations deactivated, and that msgid is exactly what the write site stores.
        assert msg.english(key) or "%(" in str(scaffold)


def test_an_unknown_or_missing_key_falls_back_and_never_blanks():
    assert msg.render("", None, "stored English") == "stored English"
    assert msg.render("retired.key", {}, "stored English") == "stored English"
    # A translator who mangles a %(slot)s must not blank or crash the sentence.
    assert msg.render("flag.stale_pair", {"wrong": 1}, "stored English") != ""
