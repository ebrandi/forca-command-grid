"""Seam B: raffle prose that a **beat worker writes into the database** and other pilots read.

``RaffleTicketLedgerEntry.reason`` and ``RaffleSuspiciousActivityFlag.detail`` are composed by
Celery beat tasks (``process_sources`` / ``integrity_scan``). A beat worker has no request, no
user and no locale — it runs under English. So wrapping those sentences in ``gettext`` /
``gettext_lazy`` cannot work: Django coerces the lazy proxy to ``str`` on write, the row is
frozen in the *writer's* locale, and every reader in every language sees that frozen English
forever. ``makemessages`` would still extract the msgid and the .po would still fill up — the
translation would simply never appear. That silent failure is what these tests pin.

The fix under test: persist a stable scaffold **key + JSON-safe params** next to the English
prose, and re-resolve the sentence per reader at render time. Each test therefore:

1. writes the row through the REAL code path, under English, exactly as the beat does; then
2. reads it back under ``translation.override("de")`` with the msgstr seeded — and asserts it
   renders GERMAN.

Seeding the msgstr is what a translator filling in that .po entry does. Without it the shipped
``de`` catalogue returns the English msgid unchanged and the bug stays invisible: the assertion
would pass against a completely broken implementation. ``_translated_de`` (reused from
``tests/test_campaigns_services``) is what makes the latent breakage reproducible *now*.
"""
from __future__ import annotations

import pytest
from django.utils import translation

from apps.raffle import engine, integrity, services
from apps.raffle.models import RaffleSuspiciousActivityFlag, RaffleTicketLedgerEntry
from tests._raffle_utils import (
    HOME_CORP,
    enrol_pilot,
    home_kill,
    make_contest,
)
from tests.test_campaigns_services import _translated_de


# ===========================================================================
#  RaffleTicketLedgerEntry.reason  (written by the process_sources beat)
# ===========================================================================
@pytest.mark.django_db
def test_ledger_reason_written_by_worker_renders_in_the_readers_locale(django_user_model):
    """The whole point: worker writes under English, a German pilot reads GERMAN."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    home_kill(5001, is_solo=True, attackers=[(1001, HOME_CORP, True)])

    # --- WRITE: the real engine path, under English, exactly as the beat runs it.
    engine.process_source(contest, "pvp")
    entry = RaffleTicketLedgerEntry.objects.get(contest=contest, character_id=1001)

    # English is unchanged — the prose column is still the audit record and the fallback.
    assert entry.reason == "Solo kill (100)"
    # ...and the translatable form rode along, as plain JSON-safe values (no lazy proxies:
    # a gettext_lazy proxy in a JSONField is a hard TypeError at save time).
    assert entry.reason_key == "pvp.solo_kill"
    assert entry.reason_params == {"tickets": 100}

    # --- READ: a different pilot, on a German request.
    with _translated_de(**{"Solo kill (%(tickets)s)": "Solokill (%(tickets)s)"}):
        assert entry.reason_i18n == "Solokill (100)"

    # The English reader is unaffected.
    with translation.override("en"):
        assert entry.reason_i18n == "Solo kill (100)"


@pytest.mark.django_db
def test_legacy_ledger_row_with_no_key_renders_its_stored_english_verbatim():
    """Nothing is backfilled. A row written before this change has no key — it must degrade to
    its stored English, never to blank (a blank ticket reason would be a data-loss bug)."""
    contest = make_contest(seed_sources=False)
    legacy = RaffleTicketLedgerEntry.objects.create(
        contest=contest, character_id=1001, source_key="pvp", source_ref="killmail:1",
        amount=100, reason="Solo kill (100)",  # ...and no reason_key / reason_params.
    )
    assert legacy.reason_key == ""
    assert legacy.reason_params == {}

    with _translated_de(**{"Solo kill (%(tickets)s)": "Solokill (%(tickets)s)"}):
        assert legacy.reason_i18n == "Solo kill (100)"


@pytest.mark.django_db
def test_interpolated_params_are_never_translated(django_user_model):
    """The i18n boundary sits between the scaffold sentence and the substituted value.

    An officer's free-text reversal note is human content: the "Reversal:" prefix is our prose
    and translates, the note itself is interpolated RAW and rendered verbatim in every locale.
    """
    contest = make_contest(seed_sources=False)
    officer, _c = enrol_pilot(django_user_model, 2001, username="officer")
    entry = RaffleTicketLedgerEntry.objects.create(
        contest=contest, character_id=1001, source_key="pvp", source_ref="killmail:1",
        amount=100, reason="Solo kill (100)", reason_key="pvp.solo_kill",
        reason_params={"tickets": 100},
    )

    reversal = services.reverse_entry(entry, officer, reason="Duplicate killmail")

    assert reversal.reason == "Reversal: Duplicate killmail"  # English unchanged
    assert reversal.reason_key == "ledger.reversal"

    with _translated_de(**{"Reversal: %(reason)s": "Stornierung: %(reason)s"}):
        # The sentence is German; the officer's note is untouched.
        assert reversal.reason_i18n == "Stornierung: Duplicate killmail"


# ===========================================================================
#  RaffleSuspiciousActivityFlag.detail  (written by the integrity_scan beat)
# ===========================================================================
@pytest.mark.django_db
def test_flag_detail_written_by_worker_renders_in_the_reviewing_officers_locale(django_user_model):
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    # A kill worth well under the 1,000,000 ISK low-value threshold.
    home_kill(5001, is_solo=True, attackers=[(1001, HOME_CORP, True)], value="500000")

    # --- WRITE: both beats, under English.
    engine.process_source(contest, "pvp")
    assert integrity.scan_contest(contest) == 1
    flag = RaffleSuspiciousActivityFlag.objects.get(contest=contest)

    assert flag.flag_type == RaffleSuspiciousActivityFlag.FlagType.LOW_VALUE
    assert flag.detail == "Kill worth 500,000 ISK (< 1,000,000)."  # English unchanged
    assert flag.detail_key == "integrity.low_value"
    assert flag.detail_params == {"value": "500,000", "limit": "1,000,000"}

    # --- READ: a German officer on the raffle_flags console.
    with _translated_de(**{
        "Kill worth %(value)s ISK (< %(limit)s).": "Kill im Wert von %(value)s ISK (< %(limit)s).",
    }):
        assert flag.detail_i18n == "Kill im Wert von 500,000 ISK (< 1,000,000)."


@pytest.mark.django_db
def test_legacy_flag_with_no_key_renders_its_stored_english_verbatim():
    contest = make_contest(seed_sources=False)
    legacy = RaffleSuspiciousActivityFlag.objects.create(
        contest=contest, character_id=1001,
        flag_type=RaffleSuspiciousActivityFlag.FlagType.LOW_VALUE,
        detail="Kill worth 500,000 ISK (< 1,000,000).",  # ...and no detail_key.
    )
    assert legacy.detail_key == ""

    with _translated_de(**{
        "Kill worth %(value)s ISK (< %(limit)s).": "Kill im Wert von %(value)s ISK (< %(limit)s).",
    }):
        assert legacy.detail_i18n == "Kill worth 500,000 ISK (< 1,000,000)."


# ===========================================================================
#  The renderer must never blank a reason / detail
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize(
    "key,params",
    [
        ("pvp.no_such_key_anymore", {"tickets": 100}),  # a key renamed/removed by a later deploy
        ("pvp.solo_kill", {"wrong_slot": 100}),         # key and params drifted apart
        ("pvp.solo_kill", {}),                          # params lost
    ],
)
def test_unresolvable_scaffold_degrades_to_stored_english_never_blank(key, params):
    contest = make_contest(seed_sources=False)
    entry = RaffleTicketLedgerEntry.objects.create(
        contest=contest, character_id=1001, source_key="pvp", source_ref="killmail:1",
        amount=100, reason="Solo kill (100)", reason_key=key, reason_params=params,
    )
    with _translated_de(**{"Solo kill (%(tickets)s)": "Solokill (%(tickets)s)"}):
        assert entry.reason_i18n == "Solo kill (100)"
