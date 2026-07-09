"""Deterministic battle after-action facts (Combat Intelligence).

Takes a killboard ``BattleReport`` (a system+window cluster of killmails) and computes the
full fact set an after-action review needs — sides, ISK swing / outcome, ship composition,
our losses (named for our own pilots, honouring recognition opt-outs), doctrine adherence,
logi losses, and a timeline — from ``Killmail`` / ``KillmailParticipant`` / ``FitDeviation``,
with ship / system / pilot ids resolved to names. The LLM only *narrates* these facts (the
two-layer rule); the grounding index is derived from them so the AAR can cite nothing that
isn't here. Own-corp pilot naming is a leadership decision (config ``battle.name_own_pilots``)
and always yields to a member's public-recognition opt-out.
"""
from __future__ import annotations

# EVE ship group names that mark a logistics (repair) hull — losing these is a key AAR signal.
_LOGI_GROUPS = {"Logistics", "Logistics Frigate", "Force Auxiliary"}
# Kept at/under the prompt sanitizer's per-list cap (50) so the LLM narrates the SAME facts
# that are stored + displayed (no silent truncation of what the model sees).
_MAX_LOSS_DETAIL = 50
_MAX_TIMELINE = 50
_MAX_KILLMAILS = 500   # bound worker memory/time for a pathologically large fight


def _iso(dt):
    return dt.isoformat() if dt else None


def battle_facts(report) -> dict:
    """Compute the deterministic after-action fact set for one battle report."""
    from django.conf import settings

    from apps.corporation.models import EveName
    from apps.sde.models import SdeSolarSystem, SdeType

    from .config import get as cfg_get
    from .snapshot import _recognition_optout_ids

    bcfg = cfg_get("battle")
    name_own = bool(bcfg.get("name_own_pilots", True))
    respect_optout = bool(bcfg.get("respect_recognition_optout", True))
    optout_ids = _recognition_optout_ids() if respect_optout else set()
    home_corp = int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)

    kms = list(
        report.killmails.all()
        .prefetch_related("participants")
        .select_related("fit_deviation")
        .order_by("killmail_time")[:_MAX_KILLMAILS]
    )

    # --- batch name resolution -------------------------------------------------
    type_ids: set[int] = set()
    entity_ids: set[int] = set()
    for km in kms:
        type_ids.add(km.victim_ship_type_id)
        if km.victim_character_id:
            entity_ids.add(km.victim_character_id)
        if km.victim_corporation_id:
            entity_ids.add(km.victim_corporation_id)
        for fd_item in _deviation_missing(km):
            type_ids.add(fd_item)
        for p in km.participants.all():
            if p.ship_type_id:
                type_ids.add(p.ship_type_id)
            if p.character_id:
                entity_ids.add(p.character_id)
            if p.corporation_id:
                entity_ids.add(p.corporation_id)
            if p.alliance_id:
                entity_ids.add(p.alliance_id)

    type_rows = SdeType.objects.filter(type_id__in=type_ids).select_related("group").values(
        "type_id", "name", "group__name"
    )
    type_name = {r["type_id"]: r["name"] for r in type_rows}
    type_group = {r["type_id"]: r["group__name"] for r in type_rows}
    name = dict(EveName.objects.filter(entity_id__in=entity_ids).values_list("entity_id", "name"))
    sys_name = dict(
        SdeSolarSystem.objects.filter(system_id__in=(report.system_ids or [])).values_list("system_id", "name")
    )
    systems = [sys_name.get(s, f"system {s}") for s in (report.system_ids or [])]

    def _tname(tid):
        return type_name.get(tid) or f"type {tid}"

    def _pilot_handle(char_id):
        if not char_id:
            return "(unknown)"
        if not name_own or char_id in optout_ids:
            return f"Pilot #{char_id % 10000}"  # pseudonymous, stable-ish handle
        return name.get(char_id) or f"char {char_id}"

    def _enemy(char_id, corp_id):
        return name.get(char_id) or name.get(corp_id) or "hostile"

    def _attacker_name(fb):
        """Name the final-blow attacker; an own-corp (awox/blue-on-blue) killer is
        pseudonymized like any of our pilots so a friendly-fire loss can't reveal a name."""
        if not fb:
            return ""
        if fb.get("corp") == home_corp:
            return _pilot_handle(fb.get("char"))
        return _enemy(fb.get("char"), fb.get("corp"))

    # --- walk the killmails ----------------------------------------------------
    our_losses: list[dict] = []
    our_ships_lost: dict[str, int] = {}
    their_ships_lost: dict[str, int] = {}
    their_losses_detail: list[dict] = []
    enemy_comp: dict[str, set[int]] = {}
    timeline: list[dict] = []
    home_isk_lost = 0.0
    home_isk_destroyed = 0.0
    logi_lost = 0
    off_doctrine = 0
    doctrine_losses = 0

    for km in kms:
        vship = _tname(km.victim_ship_type_id)
        vgroup = type_group.get(km.victim_ship_type_id) or ""
        isk = float(km.total_value or 0)
        role = km.home_corp_role

        if role == "victim":
            home_isk_lost += isk
            our_ships_lost[vship] = our_ships_lost.get(vship, 0) + 1
            if vgroup in _LOGI_GROUPS:
                logi_lost += 1
            fb = _final_blow(km)
            fd = getattr(km, "fit_deviation", None)
            is_off = fd is not None and not fd.is_clean
            if km.doctrine_fit_id:
                doctrine_losses += 1
                if is_off:
                    off_doctrine += 1
            missing = [_tname(m) for m in _deviation_missing(km)][:8] if is_off else []
            if len(our_losses) < _MAX_LOSS_DETAIL:
                our_losses.append({
                    "pilot": _pilot_handle(km.victim_character_id),
                    "ship": vship, "ship_class": vgroup, "isk": round(isk, 0),
                    "off_doctrine": is_off, "missing_modules": missing,
                    "killed_by": _attacker_name(fb),
                    "killed_by_ship": _tname(fb["ship"]) if fb and fb.get("ship") else "",
                    "time": _iso(km.killmail_time),
                })
            # what they brought: attacker ship composition on our losses
            for p in km.participants.all():
                if p.role == "attacker" and p.ship_type_id:
                    enemy_comp.setdefault(_tname(p.ship_type_id), set()).add(p.character_id or 0)
            if len(timeline) < _MAX_TIMELINE:
                timeline.append({"time": _iso(km.killmail_time), "event": "we lost",
                                 "ship": vship, "pilot": _pilot_handle(km.victim_character_id)})

        elif role == "attacker":
            home_isk_destroyed += isk
            their_ships_lost[vship] = their_ships_lost.get(vship, 0) + 1
            if len(their_losses_detail) < _MAX_LOSS_DETAIL:
                their_losses_detail.append({
                    "ship": vship, "isk": round(isk, 0),
                    "corp": name.get(km.victim_corporation_id) or "hostile",
                    "time": _iso(km.killmail_time),
                })
            if len(timeline) < _MAX_TIMELINE:
                timeline.append({"time": _iso(km.killmail_time), "event": "we killed",
                                 "ship": vship, "pilot": None})

    swing = round(home_isk_destroyed - home_isk_lost, 0)
    outcome = "favorable" if swing > 0 else ("unfavorable" if swing < 0 else "even")

    return {
        "battle_id": report.pk,
        "title": report.title,
        "systems": systems,
        "start": _iso(report.start_time),
        "end": _iso(report.end_time),
        "duration_minutes": _duration_minutes(report),
        "killmail_count": len(kms),
        "outcome": outcome,
        "totals": {
            "our_losses": sum(our_ships_lost.values()),
            "our_kills": sum(their_ships_lost.values()),
            "isk_lost": round(home_isk_lost, 0),
            "isk_destroyed": round(home_isk_destroyed, 0),
            "isk_swing": swing,
            "logi_lost": logi_lost,
            "doctrine_losses": doctrine_losses,
            "off_doctrine_losses": off_doctrine,
        },
        "our_ships_lost": _top_counts(our_ships_lost),
        "our_losses_detail": our_losses,
        "their_ships_lost": _top_counts(their_ships_lost),
        "enemy_composition": _top_counts({k: len(v) for k, v in enemy_comp.items()}),
        "timeline": timeline,
    }


def _duration_minutes(report) -> int:
    if report.start_time and report.end_time:
        return round((report.end_time - report.start_time).total_seconds() / 60.0)
    return 0


def _deviation_missing(km) -> list[int]:
    fd = getattr(km, "fit_deviation", None)
    if fd is None:
        return []
    return [m.get("type_id") for m in (fd.missing or []) if isinstance(m, dict) and m.get("type_id")]


def _final_blow(km) -> dict | None:
    for p in km.participants.all():
        if p.role == "attacker" and p.final_blow:
            return {"char": p.character_id, "corp": p.corporation_id, "ship": p.ship_type_id}
    return None


def _top_counts(counts: dict[str, int], limit: int = 15) -> list[dict]:
    return [
        {"ship": ship, "count": n}
        for ship, n in sorted(counts.items(), key=lambda x: -x[1])[:limit]
    ]


def grounding_index(facts: dict) -> dict:
    """Valid ship names / pilot handles / systems the AAR narrative may cite."""
    ships: set[str] = set()
    pilots: set[str] = set()
    for row in facts.get("our_ships_lost", []) + facts.get("their_ships_lost", []) + facts.get("enemy_composition", []):
        ships.add(row["ship"])
    for loss in facts.get("our_losses_detail", []):
        ships.add(loss["ship"])
        pilots.add(loss["pilot"])
        for m in loss.get("missing_modules", []):
            ships.add(m)
    return {"ships": ships, "pilots": pilots, "systems": set(facts.get("systems") or [])}
