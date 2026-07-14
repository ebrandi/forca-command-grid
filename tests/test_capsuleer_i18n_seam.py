"""The persisted-prose i18n seam of capsuleer ("Seam B" — ``apps.capsuleer.messages``).

``CareerMilestone.data_source`` and ``PathSuggestion.title`` / ``.reason`` are prose *written into
the database by a Celery worker* (the hourly reconcile sweep, the daily suggestion beat) and read
back later by a pilot in their own language. The worker has no reader and no locale, so a
``gettext_lazy`` at the write site is coerced to ``str`` on ``.save()`` and freezes the row in
English forever — a naive ``_()`` there passes ``makemessages`` and translates *nothing*.

So each row also carries the message-scaffold key + its raw params, and the read site
(``*_i18n``) re-renders the sentence under the READER's locale. These tests write through the real
beat bodies under English — exactly as the worker does — and then read the row back under
``de`` with a seeded msgstr. Asserting only that the columns exist would prove nothing: the whole
point is that the *stored* row renders German to a German reader while a legacy row (written before
the seam existed, hence carrying no key) still renders its stored English, verbatim and never blank.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django.utils.translation import override

from apps.capsuleer import messages, progress, services, suggest
from apps.capsuleer.models import (
    CareerGoal,
    CareerMilestone,
    GoalStatus,
    GoalType,
    MilestoneKind,
    PathSuggestion,
    SuggestionKind,
    Verification,
)
from apps.stockpile.models import Asset

from ._capsuleer_utils import _character, _goal, _member, _milestone
from .test_campaigns_services import _translated_de

pytestmark = pytest.mark.django_db

HULL = 11985


def _pilot(django_user_model, cid=7701):
    user = _member(django_user_model, str(cid))
    return user, _character(user, cid, "Seam Pilot")


# --------------------------------------------------------------------------- #
#  CareerMilestone.data_source — stamped by the reconcile sweep (a Celery beat)
# --------------------------------------------------------------------------- #
def test_sweep_stamped_provenance_renders_in_the_readers_locale(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, goal_type=GoalType.CUSTOM, status=GoalStatus.ACTIVE)
    ms = _milestone(goal, kind=MilestoneKind.SHIP_OWNED, verification=Verification.AUTO,
                    params={"type_ids": [HULL]})
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=char.character_id,
                         type_id=HULL, quantity=1)

    # The write: the hourly beat body, in a worker — no request, no reader, no locale.
    services.run_reconcile_sweep()

    ms.refresh_from_db()
    # The prose column still holds the English audit record, exactly as before this change…
    assert ms.data_source.startswith("asset mirror as of ")
    assert ms.data_source.endswith(" UTC")
    # …and the row now also carries the scaffold that produced it, with JSON-safe params only.
    assert ms.data_source_key == messages.SRC_ASSET_MIRROR
    as_of = ms.data_source_params["as_of"]
    assert isinstance(as_of, str) and as_of
    assert ms.data_source == f"asset mirror as of {as_of} UTC"

    # The read, by a German pilot: the sentence is re-rendered from the key, NOT from the frozen
    # English column. (Pre-fix there was no key and this was English forever.)
    with _translated_de(**{"asset mirror as of %(as_of)s UTC": "Asset-Spiegel vom %(as_of)s UTC"}):
        assert ms.data_source_i18n == f"Asset-Spiegel vom {as_of} UTC"

    # English output is unchanged for an English reader.
    with override("en"):
        assert ms.data_source_i18n == ms.data_source


def test_legacy_milestone_without_a_key_renders_its_stored_english(django_user_model):
    """A row stamped before this migration has no key: it degrades to its stored English, never to
    a blank line and never to a msgid."""
    user, char = _pilot(django_user_model, 7702)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    ms = _milestone(goal, kind=MilestoneKind.SHIP_OWNED, verification=Verification.AUTO,
                    params={"type_ids": [HULL]})
    CareerMilestone.objects.filter(pk=ms.pk).update(
        data_source="asset mirror as of 2020-01-01 00:00 UTC",  # legacy prose
        data_source_key="", data_source_params={},              # …with no scaffold
    )
    ms.refresh_from_db()

    with _translated_de(**{"asset mirror as of %(as_of)s UTC": "Asset-Spiegel vom %(as_of)s UTC"}):
        assert ms.data_source_i18n == "asset mirror as of 2020-01-01 00:00 UTC"


def test_template_instantiation_stamps_a_structural_blocker_key(django_user_model):
    """The ``$doctrine``-unresolved blocker is prose too — written at instantiation, read on the
    goal page and quoted inside a ``blocked_prereq`` suggestion."""
    from apps.capsuleer import plan

    user, char = _pilot(django_user_model, 7703)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    ms = _milestone(goal, kind=MilestoneKind.DOCTRINE_READY, verification=Verification.AUTO,
                    params={"unresolved": True, "tier": "viable"})
    ms.structural_block = True
    ms.data_source, ms.data_source_key, ms.data_source_params = plan._note(
        messages.SRC_NO_DOCTRINE
    ).values()
    ms.save()

    assert ms.data_source == "no matching doctrine available"

    # derive_blocked resolves under the *reader's* locale on the request path (live=False).
    with _translated_de(**{"no matching doctrine available": "keine passende Doktrin verfügbar"}):
        blocked, reasons = progress.derive_blocked(goal, live=False)
        assert blocked and reasons == ["keine passende Doktrin verfügbar"]
    with override("en"):
        assert progress.derive_blocked(goal, live=False)[1] == ["no matching doctrine available"]


# --------------------------------------------------------------------------- #
#  PathSuggestion.title / .reason — written by the daily suggestion beat
# --------------------------------------------------------------------------- #
def _stall(goal, days=60):
    old = timezone.now() - timedelta(days=days)
    CareerGoal.objects.filter(pk=goal.pk).update(created_at=old)
    goal.activity_log.update(created_at=old)
    goal.snapshots.update(taken_at=old)


def test_beat_written_suggestion_renders_in_the_readers_locale(django_user_model):
    user, char = _pilot(django_user_model, 7704)
    goal = _goal(user, character=char, title="Fly logistics", status=GoalStatus.ACTIVE)
    _stall(goal)

    # The write: the daily beat body, in a worker — English, no reader.
    suggest.run_generation()

    row = PathSuggestion.objects.get(user=user, kind=SuggestionKind.STALLED_GOAL)
    # The English prose columns are unchanged — they stay the audit record and the fallback.
    assert row.title == "«Fly logistics» hasn't moved lately"
    assert row.reason.startswith("«Fly logistics» hasn't moved in about 8 week(s).")
    # …and the scaffolds behind them are persisted, params JSON-safe (no lazy proxy survives a
    # JSONField write — that would have been a TypeError at save time).
    assert row.title_key == f"{messages.SUG_STALLED_GOAL}.title"
    assert row.title_params == {"goal": "Fly logistics"}
    assert row.reason_key == f"{messages.SUG_STALLED_GOAL}.reason"
    assert row.reason_params == {"goal": "Fly logistics", "weeks": 8}

    # The read, by a German pilot. The goal title is the pilot's own words and stays RAW inside the
    # translated sentence — the i18n boundary is the scaffold, not the interpolated value.
    with _translated_de(**{
        "«%(goal)s» hasn't moved lately": "«%(goal)s» bewegt sich nicht mehr",
        "«%(goal)s» hasn't moved in about %(weeks)s week(s). That's "
        "completely fine — interests change. If it helps, you could lower its "
        "priority, pause it, or adjust the target date.":
            "«%(goal)s» ruht seit etwa %(weeks)s Woche(n). Das ist völlig in Ordnung.",
    }):
        assert row.title_i18n == "«Fly logistics» bewegt sich nicht mehr"
        assert row.reason_i18n == "«Fly logistics» ruht seit etwa 8 Woche(n). Das ist völlig in Ordnung."

    with override("en"):
        assert row.title_i18n == row.title
        assert row.reason_i18n == row.reason


def test_legacy_suggestion_without_a_key_renders_its_stored_english(django_user_model):
    user, _char = _pilot(django_user_model, 7705)
    row = PathSuggestion.objects.create(
        user=user, kind=SuggestionKind.STALLED_GOAL, title="«Fly logistics» hasn't moved lately",
        reason="«Fly logistics» hasn't moved in about 8 week(s).", dedupe_key="legacy:1",
    )

    assert row.title_key == "" and row.title_params == {}
    with _translated_de(**{"«%(goal)s» hasn't moved lately": "«%(goal)s» bewegt sich nicht mehr"}):
        assert row.title_i18n == "«Fly logistics» hasn't moved lately"
        assert row.reason_i18n == "«Fly logistics» hasn't moved in about 8 week(s)."


def test_blocked_suggestion_localises_its_nested_blockers(django_user_model):
    """The blockers quoted inside a ``blocked_prereq`` reason are scaffold refs, not the sweep's
    frozen English — so ``%(blockers)s`` follows the reader too."""
    from apps.capsuleer import plan

    user, char = _pilot(django_user_model, 7706)
    goal = _goal(user, character=char, title="Fly logistics", status=GoalStatus.ACTIVE)
    ms = _milestone(goal, kind=MilestoneKind.DOCTRINE_READY, verification=Verification.AUTO,
                    params={"unresolved": True, "tier": "viable"})
    note = plan._note(messages.SRC_NO_DOCTRINE)
    CareerMilestone.objects.filter(pk=ms.pk).update(
        structural_block=True, data_source=note["text"], data_source_key=note["key"],
        data_source_params=note["params"],
    )

    suggest.run_generation()

    row = PathSuggestion.objects.get(user=user, kind=SuggestionKind.BLOCKED_PREREQ)
    assert row.reason.startswith("«Fly logistics» is blocked: no matching doctrine available.")
    assert row.reason_params["blockers"] == [
        {"text": "no matching doctrine available", "key": messages.SRC_NO_DOCTRINE, "params": {}}
    ]

    with _translated_de(**{
        "no matching doctrine available": "keine passende Doktrin verfügbar",
        "«%(goal)s» is blocked: %(blockers)s. You can edit the milestone, "
        "pick a different doctrine, or keep the goal parked until this resolves — "
        "nothing expires.": "«%(goal)s» ist blockiert: %(blockers)s.",
    }):
        assert row.reason_i18n == "«Fly logistics» ist blockiert: keine passende Doktrin verfügbar."


# --------------------------------------------------------------------------- #
#  The resolver itself
# --------------------------------------------------------------------------- #
def test_text_never_returns_blank():
    """Every fallback path yields *something*: an unknown key, a params/msgid mismatch and an empty
    key all degrade to the stored English rather than to a blank cell."""
    assert messages.text("stored", "no.such.key", {}) == "stored"
    assert messages.text("stored", messages.SRC_ASSET_MIRROR, {"wrong": "param"}) == "stored"
    assert messages.text("stored", "", {}) == "stored"
    assert messages.render("no.such.key") == ""
