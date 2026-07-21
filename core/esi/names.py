"""Resolve EVE entity ids ↔ names via ESI's public /universe/ endpoints (no token).

``resolve_ids`` (id → name) is a bulk lookup called only from background
tasks/commands. ``resolve_character_id`` (name → id) is a single interactive
lookup — safe to call synchronously from a deliberate user action (e.g. a
recruiter adding a candidate by name) so they get immediate feedback.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings

log = logging.getLogger("forca.names")

_CHUNK = 900  # ESI caps /universe/names/ at 1000 ids


def _esi_headers() -> dict:
    return {
        "User-Agent": settings.ESI_USER_AGENT,
        "X-Compatibility-Date": settings.ESI_COMPATIBILITY_DATE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def resolve_character_id(name: str) -> tuple[int, str] | None:
    """Resolve a single character NAME to ``(id, canonical_name)`` via /universe/ids/.

    Returns ``None`` if no character matches the name. Raises
    ``requests.RequestException`` on a transport error so the caller can tell the
    difference between "not found" and "couldn't reach EVE".
    """
    name = (name or "").strip()
    if not name:
        return None
    resp = requests.post(
        f"{settings.ESI_BASE_URL}/universe/ids/",
        json=[name],
        headers=_esi_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    characters = resp.json().get("characters") or []
    if not characters:
        return None
    # Prefer an exact (case-insensitive) match; else the first candidate.
    lowered = name.lower()
    for c in characters:
        if (c.get("name") or "").lower() == lowered:
            return c["id"], c["name"]
    return characters[0]["id"], characters[0]["name"]


# ESI /universe/ids/ accepts an array of names; keep batches well under the endpoint's
# limit so one paste of a big local never trips a 400.
_IDS_CHUNK = 300
# /characters/affiliation/ accepts up to 1000 character ids per POST.
_AFFIL_CHUNK = 1000


def resolve_character_ids(names) -> dict[str, tuple[int, str]]:
    """Bulk character NAME → ``(id, canonical_name)`` via /universe/ids/ (public, no token).

    The batched sibling of :func:`resolve_character_id`, for the D-scan/Local paste analyzer:
    one POST per :data:`_IDS_CHUNK` names, **characters only** (a Local chat member list is
    pilots — corp/alliance entries in the response are ignored). The map is keyed by the
    *lower-cased* name so a caller can align results to the exact lines it pasted, tolerating
    case; names ESI can't resolve simply don't appear (the caller lists those as unresolved).
    Resolved characters are upserted into :class:`EveName` so later pages (adversary links,
    threat tables) show real names without re-hitting ESI. Best-effort: a transport error on a
    chunk drops only that chunk (partial results beat none for a live intel call).
    """
    from apps.corporation.models import EveName

    wanted, seen = [], set()
    for raw in names or []:
        nm = (raw or "").strip()
        low = nm.lower()
        if nm and low not in seen:
            seen.add(low)
            wanted.append(nm)
    if not wanted:
        return {}

    out: dict[str, tuple[int, str]] = {}
    for start in range(0, len(wanted), _IDS_CHUNK):
        batch = wanted[start : start + _IDS_CHUNK]
        try:
            resp = requests.post(
                f"{settings.ESI_BASE_URL}/universe/ids/",
                json=batch,
                headers=_esi_headers(),
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("bulk name→id resolution failed for a chunk: %s", exc)
            continue
        for c in resp.json().get("characters") or []:
            cid, cname = c.get("id"), c.get("name") or ""
            if cid and cname:
                out[cname.lower()] = (cid, cname)
                EveName.objects.update_or_create(
                    entity_id=cid, defaults={"name": cname, "category": "character"}
                )
    return out


def character_affiliations(character_ids) -> dict[int, dict]:
    """Bulk ``character_id`` → ``{corporation_id, alliance_id, faction_id}`` via
    POST /characters/affiliation/ (public, no token; ≤ :data:`_AFFIL_CHUNK` ids per call).

    Powers the Local analyzer's corp/alliance breakdown of pasted hostiles. Best-effort:
    a failed chunk is skipped (its characters just carry no affiliation), never raised.
    """
    wanted = sorted({int(i) for i in character_ids if i})
    if not wanted:
        return {}
    out: dict[int, dict] = {}
    for start in range(0, len(wanted), _AFFIL_CHUNK):
        batch = wanted[start : start + _AFFIL_CHUNK]
        try:
            resp = requests.post(
                f"{settings.ESI_BASE_URL}/characters/affiliation/",
                json=batch,
                headers=_esi_headers(),
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("affiliation resolution failed for a chunk: %s", exc)
            continue
        for row in resp.json() or []:
            cid = row.get("character_id")
            if cid:
                out[cid] = {
                    "corporation_id": row.get("corporation_id"),
                    "alliance_id": row.get("alliance_id"),
                    "faction_id": row.get("faction_id"),
                }
    return out


def names_for(ids) -> dict[int, str]:
    """DB-only ``{id: name}`` map for entity ids — **no network**.

    Reads the resolved-name table (:class:`EveName`, kept fresh by the
    ``killboard.resolve_names`` beat) first, then falls back to the corp
    member-tracking roster (:class:`CorpMember`) for any id it still lacks. Safe
    to call from a request or a Celery sweep: it never hits ESI. Ids with no known
    name simply don't appear in the returned dict.
    """
    from apps.corporation.models import CorpMember, EveName

    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    out: dict[int, str] = {
        eid: nm
        for eid, nm in EveName.objects.filter(entity_id__in=wanted).values_list("entity_id", "name")
        if nm
    }
    missing = [c for c in wanted if c not in out]
    if missing:
        for cid, nm in CorpMember.objects.filter(character_id__in=missing).values_list(
            "character_id", "name"
        ):
            if nm:
                out[cid] = nm
    return out


def resolve_ids(ids) -> int:
    """Resolve and store names for any ids not already known. Returns count added."""
    from apps.corporation.models import EveName

    wanted = {int(i) for i in ids if i}
    if not wanted:
        return 0
    known = set(EveName.objects.filter(entity_id__in=wanted).values_list("entity_id", flat=True))
    missing = sorted(wanted - known)
    if not missing:
        return 0

    added = 0
    for start in range(0, len(missing), _CHUNK):
        added += _resolve_batch(missing[start : start + _CHUNK])
    return added


def _resolve_batch(batch: list[int]) -> int:
    """POST one batch to /universe/names/ and store the names; returns count added.

    ESI fails the WHOLE request with 404 if even a single id is unresolvable
    (a player structure used as a location, a biomassed character, …). Left
    unhandled, one bad id blocks every other name in the batch — which is why
    unlinked pilots were showing as raw ids. So on a 404 we binary-split the
    batch and retry each half, isolating the offending id(s) and dropping only
    those. Transient errors (timeouts, 5xx) are not split — we just skip the batch.
    """
    from apps.corporation.models import EveName

    if not batch:
        return 0
    try:
        resp = requests.post(
            f"{settings.ESI_BASE_URL}/universe/names/",
            json=batch,
            headers=_esi_headers(),
            timeout=30,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (400, 404):
            if len(batch) == 1:
                log.warning("dropping unresolvable id %s (HTTP %s)", batch[0], status)
                return 0
            mid = len(batch) // 2
            return _resolve_batch(batch[:mid]) + _resolve_batch(batch[mid:])
        log.warning("name resolution HTTP error for a chunk: %s", exc)
        return 0
    except requests.RequestException as exc:
        log.warning("name resolution failed for a chunk: %s", exc)
        return 0

    added = 0
    for item in resp.json():
        EveName.objects.update_or_create(
            entity_id=item["id"],
            defaults={"name": item.get("name", ""), "category": item.get("category", "")},
        )
        added += 1
    return added


def backfill_killmail_names() -> int:
    """Resolve all unresolved character/corp/alliance ids referenced by killmails.

    ``.distinct()`` is essential here: without it each ``values_list`` streams **one row
    per participant** (the participant table is millions of rows) into a Python set every
    run, when only the few tens of thousands of *distinct* entity ids matter. With it,
    Postgres returns the distinct ids from an index-only scan. ``resolve_ids`` then skips
    any id that already has an ``EveName``, so this stays correct + complete regardless."""
    from apps.killboard.models import Killmail, KillmailParticipant

    ids: set[int] = set()
    for field in ("character_id", "corporation_id", "alliance_id"):
        ids |= set(
            KillmailParticipant.objects.exclude(**{field: None})
            .values_list(field, flat=True)
            .distinct()
        )
    for field in ("victim_character_id", "victim_corporation_id", "victim_alliance_id"):
        ids |= set(
            Killmail.objects.exclude(**{field: None}).values_list(field, flat=True).distinct()
        )
    return resolve_ids(ids)
