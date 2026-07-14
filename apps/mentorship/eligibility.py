"""Mentor / mentee eligibility, computed from PUBLIC ESI data.

Two public (no-token) endpoints answer eligibility honestly:
  * ``GET /characters/{id}/``                → ``birthday`` (character age)
  * ``GET /characters/{id}/corporationhistory/`` → current-corp ``start_date`` (tenure)

Both are cached (Django cache, at/above ESI's own cache TTL) so repeat checks are
free and we never hammer ESI from a web request. When ESI is unreachable we fall
back to ``EveCharacter.added_at`` (when the pilot linked to Command Grid) as a
weak lower bound on tenure and flag ``confidence="low"`` so the UI is honest about
what it knows. Thresholds come from the leadership-tuned ``MentorshipProgram``.
"""
from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import pilots

from . import messages as msg

_AGE_TTL = 7 * 24 * 3600      # ESI character public data caches ~7d
_HIST_TTL = 24 * 3600         # corporation history caches ~1d


def _fetch_facts(character) -> dict:
    """Return ``{age_days, tenure_days, confidence, source}`` for a character.

    Cached per character. Never raises — on any ESI error it returns a fallback
    built from ``added_at`` with ``confidence="low"``.
    """
    cid = character.character_id
    ckey = f"mentorship:facts:v1:{cid}"
    cached = cache.get(ckey)
    if cached is not None:
        return cached

    now = timezone.now()
    age_days = None
    tenure_days = None
    confidence = "low"
    source = "fallback"

    try:
        from core.esi.client import get_client

        client = get_client()
        info = client.get(f"/characters/{cid}/").data or {}
        birthday = parse_datetime(info.get("birthday", "")) if info.get("birthday") else None
        if birthday:
            age_days = max(0, (now - birthday).days)

        # Corp tenure: the start_date of the current stint in the character's corp.
        corp_id = info.get("corporation_id") or character.corporation_id
        history = client.get(f"/characters/{cid}/corporationhistory/").data or []
        starts = [
            parse_datetime(rec["start_date"])
            for rec in history
            if rec.get("corporation_id") == corp_id and rec.get("start_date")
        ]
        if starts:
            tenure_days = max(0, (now - max(starts)).days)
        confidence = "high"
        source = "esi"
    except Exception:  # noqa: BLE001,S110 - eligibility degrades gracefully to fallback
        pass

    if tenure_days is None and character.added_at:
        # Weak lower bound: we know they've been linked here at least this long.
        tenure_days = max(0, (now - character.added_at).days)

    facts = {
        "age_days": age_days,
        "tenure_days": tenure_days,
        "confidence": confidence,
        "source": source,
    }
    cache.set(ckey, facts, _HIST_TTL if source == "esi" else 900)
    return facts


def _pick_character(user):
    """The pilot eligibility is judged on: the one the user is flying (LP-3)."""
    return pilots.acting_pilot(user)


def evaluate(user, program, role: str) -> dict:
    """Compute an eligibility snapshot for ``user`` as ``role`` ('mentor'|'mentee').

    Returns a JSON-serialisable dict suitable for ``MentorProfile.eligibility`` /
    ``MenteeProfile.eligibility`` and for rendering "you appear eligible because…".

    Seam B: the snapshot is *persisted* — and refreshed by the ``mentorship.refresh_eligibility``
    worker, which has no locale — then read back by the pilot and by officers under theirs. So the
    reasons are stored twice: ``reasons`` is the English prose (the audit record and the fallback
    for snapshots taken before this change) and ``reasons_i18n`` is the parallel list of scaffold
    ``{"key", "params"}`` entries that :func:`reasons_for` re-renders in the reader's locale.
    NEVER a lazy proxy: this dict goes into a JSONField, where a proxy is a TypeError at save time.
    """
    character = _pick_character(user)
    now = timezone.now()
    base = {
        "role": role,
        "eligible": False,
        "character_id": getattr(character, "character_id", None),
        "character_age_days": None,
        "corp_tenure_days": None,
        "confidence": "low",
        "source": "none",
        "reasons": [],
        "computed_at": now.isoformat(),
    }
    if character is None:
        return _with_reasons(base, [{"key": "elig.no_character", "params": {}}])

    facts = _fetch_facts(character)
    age = facts["age_days"]
    tenure = facts["tenure_days"]
    base.update(
        character_age_days=age,
        corp_tenure_days=tenure,
        confidence=facts["confidence"],
        source=facts["source"],
    )
    reasons: list[dict] = []

    if role == "mentor":
        min_age = program.mentor_min_character_age_days
        min_tenure = program.mentor_min_corp_tenure_days
        age_ok = age is not None and age >= min_age
        tenure_ok = tenure is not None and tenure >= min_tenure
        if program.mentor_eligibility_logic == program.EligibilityLogic.BOTH:
            eligible = age_ok and tenure_ok
        else:
            eligible = age_ok or tenure_ok
        if age is not None:
            reasons.append({
                "key": "elig.mentor_age_meets" if age_ok else "elig.mentor_age_below",
                "params": {"years": age // 365, "days": age % 365, "min": min_age},
            })
        else:
            reasons.append({"key": "elig.mentor_age_unknown", "params": {}})
        if tenure is not None:
            reasons.append({
                "key": "elig.mentor_tenure_meets" if tenure_ok else "elig.mentor_tenure_below",
                "params": {"days": tenure, "min": min_tenure},
            })
        else:
            reasons.append({"key": "elig.mentor_tenure_unknown", "params": {}})
        base["eligible"] = bool(eligible)

    else:  # mentee
        if not program.enforce_mentee_eligibility:
            base["eligible"] = True
            reasons.append({"key": "elig.mentee_disabled", "params": {}})
        else:
            max_tenure = program.mentee_max_corp_tenure_days
            if tenure is None:
                # Unknown tenure: allow but flag low confidence (likely a new pilot).
                base["eligible"] = True
                reasons.append({"key": "elig.mentee_tenure_unknown", "params": {}})
            else:
                eligible = tenure < max_tenure
                base["eligible"] = bool(eligible)
                reasons.append({
                    "key": "elig.mentee_tenure_under" if eligible else "elig.mentee_tenure_over",
                    "params": {"days": tenure, "max": max_tenure},
                })

    return _with_reasons(base, reasons)


def _with_reasons(snapshot: dict, entries: list[dict]) -> dict:
    """Stamp both halves of the reason list onto an eligibility snapshot."""
    snapshot["reasons"] = msg.english_list(entries)
    snapshot["reasons_i18n"] = entries
    return snapshot


def reasons_for(snapshot: dict | None) -> list[str]:
    """The snapshot's reasons under the **reader's** locale.

    A snapshot taken before this change has no ``reasons_i18n`` and falls back to its stored
    English ``reasons``, verbatim — never blank.
    """
    snapshot = snapshot or {}
    return msg.render_list(snapshot.get("reasons_i18n"), snapshot.get("reasons"))


def for_display(snapshot: dict | None) -> dict:
    """A copy of ``snapshot`` whose ``reasons`` are rendered in the reader's locale.

    Views hand this to the template instead of the raw snapshot, so the rendered page localises
    without the stored row ever being touched.
    """
    out = dict(snapshot or {})
    if out:
        out["reasons"] = reasons_for(out)
    return out


def invalidate(character_id: int) -> None:
    cache.delete(f"mentorship:facts:v1:{character_id}")
