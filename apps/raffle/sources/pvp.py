"""PVP ticket source — the primary, fully-automatic source.

Awards, per the spec's fixed precedence, for each eligible home-corp attacker on a
killmail:

* **solo kill** (only attacker)        → 100 tickets
* **final blow** on a shared kill       → 10 tickets
* **participation** on a shared kill    → 1 ticket

Idempotent by construction: one ledger row per ``(contest, "pvp", "killmail:<id>",
character_id)`` (the ledger's unique constraint), so reprocessing a killmail never
double-awards. Rich, additive filters (region / system / sec band / victim ship
type·group·category / target corp·alliance·pilot / min·max value / structures /
pods / NPC final blows / blue standings) are read from the source config so new
filters can be added without touching the engine.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.killboard.models import Killmail, KillmailParticipant
from core.esi.names import names_for

from .base import AUTOMATIC, SourceEvent, TicketSource

# Capsule (pod) hull ids and the SDE categories that denote a structure kill.
POD_TYPE_IDS = frozenset({670, 33328})
CAPSULE_GROUP_ID = 29
STRUCTURE_CATEGORY_IDS = frozenset({65, 23, 40})  # Structure / Starbase / Sovereignty


@lru_cache(maxsize=4096)
def _ship_group_category(type_id: int) -> tuple[int | None, int | None]:
    """(group_id, category_id) for a ship type, or (None, None) if SDE lacks it.

    Cached per process; the raffle beat clears process caches between runs only in
    tests, and ship→group mapping is immutable, so a long-lived cache is safe.
    """
    from apps.sde.models import SdeType

    row = (
        SdeType.objects.filter(type_id=type_id)
        .select_related("group")
        .values_list("group_id", "group__category_id")
        .first()
    )
    return row if row else (None, None)


class PvpSource(TicketSource):
    key = "pvp"
    label = _("PVP kills")
    description = _("Tickets for participating in corp kills "
                    "(solo 100 · final blow 10 · participation 1).")
    unit = _("kills")
    reliability = AUTOMATIC
    default_mode = "auto"
    default_config = {"per_kill": 1, "final_blow": 10, "solo": 100}
    default_filters = {
        "regions": [], "systems": [], "sec_bands": [],
        "victim_ship_type_ids": [], "victim_ship_group_ids": [], "victim_ship_category_ids": [],
        "target_corporation_ids": [], "target_alliance_ids": [], "target_character_ids": [],
        "max_value": None,
        "include_structures": True, "include_pods": True, "include_npc": False,
        "exclude_blue": True, "exclude_awox": True,
    }

    def _our_side_corp_ids(self, contest) -> set[int]:
        """Corp ids whose attacker pilots we credit (home corp, + service corps if
        the contest admits alliance/friendly pilots)."""
        ids = {int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)}
        if contest.include_alliance:
            from apps.corporation.access import service_corp_ids

            ids |= set(service_corp_ids())
        ids.discard(0)
        return ids

    def _passes_filters(self, km: Killmail, config, contest) -> bool:
        f = config.filters or {}
        regions = f.get("regions") or []
        if regions and km.region_id not in regions:
            return False
        systems = f.get("systems") or []
        if systems and km.solar_system_id not in systems:
            return False
        bands = f.get("sec_bands") or []
        if bands and km.sec_band not in bands:
            return False

        # min value uses the generic min_threshold; max is a PVP-specific filter.
        if config.min_threshold and km.total_value < config.min_threshold:
            return False
        max_value = f.get("max_value")
        if max_value and km.total_value > max_value:
            return False

        # Victim ship type / group / category.
        vtype = km.victim_ship_type_id
        type_ids = f.get("victim_ship_type_ids") or []
        group_ids = f.get("victim_ship_group_ids") or []
        cat_ids = f.get("victim_ship_category_ids") or []
        if type_ids and vtype not in type_ids:
            return False
        if group_ids or cat_ids or not f.get("include_structures", True):
            group_id, category_id = _ship_group_category(vtype)
            if group_ids and group_id not in group_ids:
                return False
            if cat_ids and category_id not in cat_ids:
                return False
            if not f.get("include_structures", True) and category_id in STRUCTURE_CATEGORY_IDS:
                return False

        if not f.get("include_pods", True) and vtype in POD_TYPE_IDS:
            return False
        if not f.get("include_npc", False) and km.is_npc:
            return False
        if f.get("exclude_awox", True) and km.is_awox:
            return False

        # Target (victim) filters.
        tcorp = f.get("target_corporation_ids") or []
        if tcorp and km.victim_corporation_id not in tcorp:
            return False
        tally = f.get("target_alliance_ids") or []
        if tally and km.victim_alliance_id not in tally:
            return False
        tchar = f.get("target_character_ids") or []
        if tchar and km.victim_character_id not in tchar:
            return False

        # Blue / friendly kill exclusion via corp standings (best-effort proxy).
        if f.get("exclude_blue", True) and self._is_blue(km):
            return False
        return True

    @staticmethod
    def _is_blue(km: Killmail) -> bool:
        """Whether the victim belongs to a registered partner alliance / friendly corp."""
        from apps.corporation.access import service_alliance_ids, service_corp_ids

        if km.victim_alliance_id and km.victim_alliance_id in service_alliance_ids():
            return True
        if km.victim_corporation_id and km.victim_corporation_id in service_corp_ids():
            return True
        return False

    def iter_events(self, contest, config, since, until):
        cfg = config.config or {}
        per_kill = int(cfg.get("per_kill", 1))
        final_blow_tix = int(cfg.get("final_blow", 10))
        solo_tix = int(cfg.get("solo", 100))
        our_corps = self._our_side_corp_ids(contest)
        if not our_corps:
            return
        # Alliances we also treat as "our side" when the contest admits alliance
        # pilots — mirrors apps.raffle.eligibility._corp_or_alliance so the
        # friendly-fire guard below blocks exactly whom eligibility would admit as
        # a participant (an alliance-mate whose corp isn't separately registered as
        # a friendly corp is still our side).
        our_alliances: set[int] = set()
        if contest.include_alliance:
            from apps.corporation.access import service_alliance_ids

            our_alliances = set(service_alliance_ids())

        kills = (
            Killmail.objects.filter(
                involves_home_corp=True,
                home_corp_role=Killmail.HomeRole.ATTACKER,
                killmail_time__gte=since,
                killmail_time__lt=until,
            )
            .order_by("killmail_time")
        )

        def _events_for(batch):
            """Batch the attacker lookup: one KillmailParticipant query per ~400 kills,
            not one per kill (avoids the N+1 on the 15-min re-scan)."""
            km_by_id = {km.killmail_id: km for km in batch}
            attackers = defaultdict(list)
            for a in KillmailParticipant.objects.filter(
                killmail_id__in=list(km_by_id), role=KillmailParticipant.Role.ATTACKER
            ).values("killmail_id", "character_id", "corporation_id", "final_blow"):
                attackers[a["killmail_id"]].append(a)
            # Resolve attacker names once per batch (DB-only, no ESI) so BOTH the
            # ledger and the ineligible-activity/outreach rows carry a real name
            # instead of a blank — killmail participants store only the id.
            names = names_for(
                a["character_id"]
                for al in attackers.values()
                for a in al
                if a["character_id"] and a["corporation_id"] in our_corps
            )
            for kid, km in km_by_id.items():
                for a in attackers.get(kid, []):
                    cid = a["character_id"]
                    if not cid or a["corporation_id"] not in our_corps:
                        continue
                    # Seam B: persist the English prose (audit + fallback) AND a scaffold
                    # key/params so each reader renders it in their own locale. Do NOT wrap
                    # ``reason`` in _() — this runs in a beat worker with no locale, and the
                    # proxy would be coerced to English on bulk_create and frozen forever.
                    if km.is_solo:
                        tickets, reason = solo_tix, f"Solo kill ({solo_tix})"
                        reason_key = "pvp.solo_kill"
                    elif a["final_blow"]:
                        tickets, reason = final_blow_tix, f"Final blow on shared kill ({final_blow_tix})"
                        reason_key = "pvp.final_blow"
                    else:
                        tickets, reason = per_kill, f"Kill participation ({per_kill})"
                        reason_key = "pvp.participation"
                    yield SourceEvent(
                        character_id=cid,
                        character_name=names.get(cid, ""),
                        source_ref=f"killmail:{km.killmail_id}",
                        base_tickets=tickets,
                        occurred_at=km.killmail_time,
                        magnitude=float(km.total_value or 0),
                        reason=reason,
                        reason_key=reason_key,
                        reason_params={"tickets": int(tickets)},
                        metadata={
                            "killmail_id": km.killmail_id,
                            "final_blow": bool(a["final_blow"]),
                            "solo": bool(km.is_solo),
                            "victim_ship_type_id": km.victim_ship_type_id,
                            "value": str(km.total_value or 0),
                        },
                    )

        batch = []
        for km in kills.iterator(chunk_size=500):
            # Never reward killing our own side. Skip the whole mail when the victim
            # belongs to a corp — or, when the contest admits alliance pilots, an
            # alliance — that we credit attackers for. This is corp-on-corp / friendly
            # fire and must never earn, deliberately independent of the toggleable
            # ``exclude_blue`` filter and the killmail's home_corp_role tag, so a
            # same-side kill can never earn a ticket by any path.
            if km.victim_corporation_id and km.victim_corporation_id in our_corps:
                continue
            if our_alliances and km.victim_alliance_id and km.victim_alliance_id in our_alliances:
                continue
            if not self._passes_filters(km, config, contest):
                continue
            batch.append(km)
            if len(batch) >= 400:
                yield from _events_for(batch)
                batch = []
        if batch:
            yield from _events_for(batch)
