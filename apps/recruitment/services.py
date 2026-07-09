"""Recruitment services: public-evidence computation and consent requests.

``build_public_evidence`` is a pure function (no I/O) so it is fully testable;
the Celery task fetches public ESI and feeds it in. Nothing here renders a
verdict — only evidence with confidence levels (PRD Principle 13).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

_log = logging.getLogger("forca.recruitment")

# How many corps in the last year before we flag churn as something to discuss.
CORP_HOP_FLAG = 5


def _years_since(iso_birthday: str, now: datetime) -> float | None:
    try:
        born = datetime.fromisoformat(iso_birthday.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return round((now - born).days / 365.25, 1)


def build_public_evidence(
    char_public: dict, corp_history: list, now: datetime, red_entities: set | None = None
) -> list[dict]:
    """Derive evidence rows from public ESI data. Pure; no network or DB.

    ``red_entities`` is the set of corporation/alliance ids our corp holds a
    *negative* standing toward (from our own contacts) — passing it lets the brief
    flag a candidate who recently flew with someone we consider hostile, the
    strongest spy/awox signal available without the candidate's own token.
    """
    rows: list[dict] = []
    red = red_entities or set()

    age = _years_since(char_public.get("birthday", ""), now)
    if age is not None:
        rows.append({
            "theme": "identity", "claim": f"Character age: {age} years",
            "confidence": "high", "source": "public", "is_flag": False,
        })

    # Security status (public) — informational; surfaced for context, not a verdict.
    sec = char_public.get("security_status")
    if sec is not None:
        rows.append({
            "theme": "identity", "claim": f"Security status: {float(sec):+.1f}",
            "confidence": "high", "source": "public", "is_flag": False,
        })

    # Corp churn over the last 12 months (public corp history).
    cutoff = now.timestamp() - 365 * 24 * 3600
    recent_corps = set()
    for entry in corp_history or []:
        start = entry.get("start_date", "")
        try:
            ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        if ts >= cutoff:
            recent_corps.add(entry.get("corporation_id"))
    n_recent = len(recent_corps)
    if corp_history:
        flagged = n_recent >= CORP_HOP_FLAG
        rows.append({
            "theme": "risk" if flagged else "identity",
            "claim": f"{n_recent} corporation(s) in the last 12 months"
            + (" — worth asking about" if flagged else ""),
            "confidence": "medium", "source": "public", "is_flag": flagged,
        })

    # Cross-check the full employment history against our red standings.
    hostile = {
        e.get("corporation_id") for e in (corp_history or [])
        if e.get("corporation_id") in red
    }
    if hostile:
        rows.append({
            "theme": "risk",
            "claim": f"Flew with {len(hostile)} corp(s) we hold red — verify before accepting",
            "confidence": "high", "source": "public", "is_flag": True,
        })

    return rows


def _fmt_sp(sp: int) -> str:
    if sp >= 1_000_000_000:
        return f"{sp / 1_000_000_000:.1f}B"
    if sp >= 1_000_000:
        return f"{sp / 1_000_000:.1f}M"
    if sp >= 1_000:
        return f"{sp / 1_000:.0f}K"
    return str(sp)


# A character holding Director in their CURRENT corp is the single most
# discussion-worthy role signal for a recruiter (leadership material — or an
# unusual move worth understanding before accepting), so it is flagged.
_FLAG_ROLE = "Director"


def build_esi_evidence(skills: dict | None, roles: dict | None) -> list[dict]:
    """Derive vetting claims from a candidate's own ESI skills + corp roles.

    Pure: no network or DB. Stores *derived claims only* — never the raw skill
    list or inventory (PRD privacy principle). ``skills``/``roles`` are the ESI
    response bodies (or None when that scope was not granted).
    """
    rows: list[dict] = []

    if skills:
        total = skills.get("total_sp")
        if total is not None:
            rows.append({
                "theme": "combat", "claim": f"Total skill points: {_fmt_sp(int(total))} (ESI-confirmed)",
                "confidence": "high", "source": "esi", "is_flag": False,
            })
        skill_list = skills.get("skills") or []
        if skill_list:
            at_v = sum(1 for s in skill_list if s.get("trained_skill_level") == 5)
            rows.append({
                "theme": "combat", "claim": f"{len(skill_list)} skills trained, {at_v} at level V",
                "confidence": "high", "source": "esi", "is_flag": False,
            })

    if roles is not None:
        held = list(roles.get("roles") or [])
        if held:
            is_dir = _FLAG_ROLE in held
            pretty = ", ".join(r.replace("_", " ") for r in held)
            rows.append({
                "theme": "roles",
                "claim": f"Holds roles in their current corp: {pretty}"
                + (" — currently a Director; confirm why they are leaving" if is_dir else ""),
                "confidence": "high", "source": "esi", "is_flag": is_dir,
            })
        else:
            rows.append({
                "theme": "roles", "claim": "No special roles in their current corp (line member)",
                "confidence": "high", "source": "esi", "is_flag": False,
            })

    return rows


def read_candidate_esi(
    character_id: int, access_token: str, granted_scopes: list[str]
) -> tuple[dict | None, dict | None]:
    """Read skills + corp roles ONCE with the candidate's token. Reads only the
    endpoints whose scope was actually granted; returns the raw ESI bodies for
    ``build_esi_evidence``. The token is the caller's to discard afterwards."""
    from core.esi.client import ESIClient

    client = ESIClient()
    skills = roles = None
    if "esi-skills.read_skills.v1" in granted_scopes:
        skills = client.get(f"/characters/{character_id}/skills/", token=access_token).data
    if "esi-characters.read_corporation_roles.v1" in granted_scopes:
        roles = client.get(f"/characters/{character_id}/roles/", token=access_token).data
    return skills, roles


def store_esi_evidence(candidate, rows: list[dict]) -> int:
    """Replace a candidate's ESI-derived evidence rows with ``rows``."""
    from django.utils import timezone

    from .models import CandidateEvidence

    CandidateEvidence.objects.filter(candidate=candidate, source="esi").delete()
    for r in rows:
        CandidateEvidence.objects.create(candidate=candidate, **r)
    candidate.evidence_refreshed_at = timezone.now()
    candidate.save(update_fields=["evidence_refreshed_at"])
    return len(rows)


def refresh_evidence(candidate) -> int:
    """Fetch public ESI for a candidate and replace their public evidence rows."""
    from django.utils import timezone

    from core.esi.client import ESIClient

    from .models import CandidateEvidence

    client = ESIClient()
    char_public = client.get(f"/characters/{candidate.character_id}/").data or {}
    corp_history = client.get(f"/characters/{candidate.character_id}/corporationhistory/").data or []
    now = datetime.now(UTC)

    # Corps/alliances we hold a negative standing toward — the hostile cross-check.
    from apps.corporation.models import Contact

    red_entities = set(
        Contact.objects.filter(standing__lt=0).values_list("contact_id", flat=True)
    )
    rows = build_public_evidence(char_public, corp_history, now, red_entities)
    CandidateEvidence.objects.filter(candidate=candidate, source="public").delete()
    for r in rows:
        CandidateEvidence.objects.create(candidate=candidate, **r)
    candidate.evidence_refreshed_at = timezone.now()
    candidate.save(update_fields=["evidence_refreshed_at"])
    return len(rows)


def request_consent(candidate, requested_by, scopes: list[str], ttl_hours: int = 168):
    """Record a scoped, time-boxed consent request for a candidate ESI link.

    Returns the CandidateConsent. The live authorize/exchange flow (a dedicated,
    non-login callback registered with the EVE app) is a separately reviewed
    step; this records the intent, scopes and expiry safely.
    """
    from datetime import timedelta

    from django.utils import timezone

    from core.esi import oauth

    from .models import CandidateConsent

    consent = CandidateConsent.objects.create(
        candidate=candidate,
        scopes=scopes,
        state=oauth.generate_state(),
        requested_by=requested_by,
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )
    return consent


def home_killboard_evidence(character_id: int) -> dict | None:
    """REC-KB-2 (3.8): a candidate's involvement in FORCA's own killboard — a cheaper, stronger
    awox/competence signal than an external deep-link.

    Three signals, all from our local (public) killmail data keyed by character id:
      * ``killed_by_us``   — times the home corp killed this pilot (they were the victim),
      * ``fought_with``    — killmails where they attacked ALONGSIDE the home corp,
      * ``fought_against`` — killmails where they attacked one of OUR pilots (a red flag).

    Returns ``None`` when the pilot appears nowhere on the board.
    """
    from django.db.models import Count, Max

    from apps.killboard.models import Killmail, KillmailParticipant

    if not character_id:
        return None
    home_att = Killmail.HomeRole.ATTACKER
    home_vic = Killmail.HomeRole.VICTIM
    att = KillmailParticipant.Role.ATTACKER

    killed_by_us = Killmail.objects.filter(
        victim_character_id=character_id, involves_home_corp=True, home_corp_role=home_att)
    fought_with = Killmail.objects.filter(
        involves_home_corp=True, home_corp_role=home_att,
        participants__character_id=character_id, participants__role=att)
    fought_against = Killmail.objects.filter(
        involves_home_corp=True, home_corp_role=home_vic,
        participants__character_id=character_id, participants__role=att)

    # One aggregate per signal (count + recency together); Count(distinct) dedupes the
    # participant join so a pilot with two attacker rows on one killmail counts it once.
    killed = killed_by_us.aggregate(n=Count("pk", distinct=True), m=Max("killmail_time"))
    withs = fought_with.aggregate(n=Count("pk", distinct=True), m=Max("killmail_time"))
    against = fought_against.aggregate(n=Count("pk", distinct=True), m=Max("killmail_time"))
    n_killed, n_with, n_against = killed["n"], withs["n"], against["n"]
    if not (n_killed or n_with or n_against):
        return None
    last_activity = max(
        (s for s in (killed["m"], withs["m"], against["m"]) if s is not None), default=None)
    return {
        "killed_by_us": n_killed,
        "fought_with": n_with,
        "fought_against": n_against,
        "last_activity": last_activity,
        "is_hostile": n_against > 0,
        "is_friendly": n_with > 0 and n_against == 0,
    }


def handoff_joined_candidate(candidate) -> dict:
    """REC-KB-3 (3.16): when a candidate is marked *joined*, route them into onboarding and
    mentorship instead of leaving the status change a dead end.

    If the pilot has signed into FORCA (a linked account), we (1) seed their onboarding
    progress so they land on their checklist, and (2) register them as a mentee — idempotently
    — so they surface on the officer matching worklist with a mentor suggested. The vetting
    context (evidence + recruiter notes) stays on the officer-only ``Candidate`` record, which
    is preserved (not purged) on join — it is deliberately NOT copied into the mentee's own
    self-visible profile.

    Best-effort and non-fatal: a downstream hiccup returns a reason rather than raising, so
    marking a candidate joined never fails. Returns a status dict.
    """
    from apps.sso.models import EveCharacter

    try:
        character = (
            EveCharacter.objects.filter(character_id=candidate.character_id)
            .select_related("user").first()
        )
    except Exception:  # noqa: BLE001 — even the lookup must not fail the status change
        _log.exception("handoff account lookup failed for candidate %s", candidate.pk)
        return {"handed_off": False, "reason": "error"}
    if character is None or character.user_id is None:
        return {"handed_off": False, "reason": "no_account"}

    user = character.user
    onboarding_started = mentee_created = False
    try:
        from apps.onboarding.services import evaluate_milestones

        evaluate_milestones(character)  # lands them on their onboarding checklist
        onboarding_started = True
    except Exception:  # noqa: BLE001 — a handoff hiccup must not fail the status change
        _log.exception("onboarding handoff failed for candidate %s", candidate.pk)

    try:
        from apps.mentorship import services as mentorship
        from apps.mentorship.models import MenteeProfile

        # Skip when the program is paused, or they're already a mentee (don't clobber a real
        # application). Register a BARE mentee so they surface on the matching worklist —
        # deliberately NOT copying the recruiter's private vetting notes (MenteeProfile.notes is
        # the mentee's OWN, self-visible field). Vetting context stays on the officer-only
        # Candidate record, preserved (not purged) on join.
        if mentorship.program_open() and not MenteeProfile.objects.filter(user=user).exists():
            mentorship.register_mentee(user, {})
            mentee_created = True
    except Exception:  # noqa: BLE001
        _log.exception("mentorship handoff failed for candidate %s", candidate.pk)

    return {
        "handed_off": True, "user_id": user.id,
        "onboarding_started": onboarding_started, "mentee_created": mentee_created,
    }
