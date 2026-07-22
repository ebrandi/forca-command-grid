"""KB-36 (WS-D2) — battle-role detection from flag-resolved fits.

Classify a fitted ship into a combat role — logi / tackle / ewar / links / dps, plus a
``capital`` hull bucket — from what it was fielding. Two very different signals:

* **Victims** — a killmail carries the victim's full item list, each flag-resolved into a
  fitting slot (:func:`apps.killboard.fitrender.slot_bucket`). We read the role straight
  from the module groups the ship had fitted. This is the authoritative signal, and we hold
  it for *every* victim on the board (our losses AND our kills — the thing we killed is the
  victim of that mail).
* **Attackers** — an ESI attacker row is only ``ship_type_id`` + ``weapon_type_id``; there
  is no module list. So an attacker's role is a coarse HULL-based approximation and we are
  honest about its limits: only logi hulls (by group) and capital hulls are inferable, and
  everything else falls to ``dps``. The weapon id only ever confirms the dps default, so it
  does not change the result today (it is accepted for future refinement).

The module→role mapping is resolved AT RUNTIME from SDE group NAMES — never a hard-coded
type-id list. We read each fitted module's ``SdeType.group.name`` and match it against the
documented group-name sets below, so a new module CCP adds to an existing group is picked up
for free. The capital flag reuses :func:`apps.doctrines.hulls.hull_class_for_group` (the same
stable group-id → class map the doctrine library already ships), and the logi-hull groups are
matched by name for symmetry with the module path.

Scoping (market-leadership plan §1/§9): everything here runs over OUR killmail history only —
the board never ingests a mail that does not touch the home corp — so a "composition" is
always our fleet vs whoever we actually fought, never a universal claim.
"""
from __future__ import annotations

from collections import defaultdict

from django.utils.translation import gettext_lazy as _

from apps.doctrines.hulls import hull_class_for_group

from .fitrender import slot_bucket

# --------------------------------------------------------------------------- #
#  Roles
# --------------------------------------------------------------------------- #
LOGI = "logi"
TACKLE = "tackle"
EWAR = "ewar"
LINKS = "links"
CAPITAL = "capital"
DPS = "dps"

# Precedence + display order (highest-precedence first). A ship that carries, say, both a
# remote rep and a scram reads as logi; a capital hull with no support module reads capital;
# a plain gun/missile boat is dps.
ROLE_ORDER = [LOGI, TACKLE, EWAR, LINKS, CAPITAL, DPS]
_ROLE_RANK = {role: i for i, role in enumerate(ROLE_ORDER)}

# User-facing labels (prose → translated). The role KEYS above are code values used in cache
# keys / template logic and are never translated.
ROLE_LABELS = {
    LOGI: _("Logi"),
    TACKLE: _("Tackle"),
    EWAR: _("EWAR"),
    LINKS: _("Links"),
    CAPITAL: _("Capital"),
    DPS: _("DPS"),
}

_CHARGE_CATEGORY = 8  # SDE category for ammo/charges — a loaded charge is not a role module

# Only real module racks carry a role module — never a rig/subsystem/drone/cargo item.
_MODULE_SLOTS = frozenset({"high", "med", "low"})

# --------------------------------------------------------------------------- #
#  Group-name taxonomy (SDE invGroups.groupName, matched case-insensitively).
#  Documented so a reviewer can audit exactly what counts as each role.
# --------------------------------------------------------------------------- #
# Remote-repair / remote-cap modules — the logistics signature.
_LOGI_GROUPS = frozenset({
    "remote armor repairer",
    "remote shield booster",
    "remote hull repairer",
    "remote capacitor transmitter",
    "ancillary remote armor repairer",
    "ancillary remote shield booster",
    "ancillary remote hull repairer",
})
# Point/hold modules. The SDE "Warp Scrambler" group holds both scram and disruptor; the
# "warp disruptor" alias is defensive in case a future SDE splits them.
_TACKLE_GROUPS = frozenset({
    "warp scrambler",
    "warp disruptor",
    "stasis web",
    "stasis grappler",
    "warp disruption field generator",  # HIC warp-disruption (bubble) generators
})
# Electronic warfare: jams, damps, paints, tracking/guidance disruption.
_EWAR_GROUPS = frozenset({
    "ecm",
    "ecm burst",
    "burst jammer",
    "remote sensor damper",
    "target painter",
    "weapon disruptor",   # modern unified tracking + guidance disruptor group
    "tracking disruptor",  # legacy group name
})
# Fleet command bursts (and the legacy warfare-link group).
_LINKS_GROUPS = frozenset({
    "command burst",
    "gang coordinator",
})

# Attacker-side hull groups we can read as logi from the hull ALONE (no module list). These
# are the dedicated logistics hull groups; a generic combat hull is never assumed to be logi.
_LOGI_HULL_GROUPS = frozenset({
    "logistics",          # T2 logistics cruisers (Guardian / Basilisk / Oneiros / Scimitar)
    "logistics frigate",  # T2 logistics frigates (Deacon / Kirin / Scalpel / Thalia)
    "force auxiliary",    # capital logistics (Apostle / Minokawa / Lif / Ninazu)
})


# --------------------------------------------------------------------------- #
#  Pure classifiers (given already-resolved SDE metadata — query-free, testable)
# --------------------------------------------------------------------------- #
def role_from_module_groups(group_names) -> str | None:
    """First role matched by a set of fitted-module group names (lower-cased), by precedence.

    Returns ``None`` when nothing matches — a pure gun/utility fit, to be resolved as
    capital-or-dps by the caller that also knows the hull.
    """
    names = {g for g in group_names if g}
    if names & _LOGI_GROUPS:
        return LOGI
    if names & _TACKLE_GROUPS:
        return TACKLE
    if names & _EWAR_GROUPS:
        return EWAR
    if names & _LINKS_GROUPS:
        return LINKS
    return None


def victim_role(module_group_names, hull_group_id) -> str:
    """The authoritative, item-based role of a victim ship.

    ``module_group_names``: lower-cased group names of the ship's fitted high/med/low modules
    (charges already excluded). ``hull_group_id``: the victim hull's SDE group id, for the
    capital fallback. Module role wins (a triage carrier fielding remote reps is logi, not
    capital); otherwise a capital hull reads ``capital`` and everything else ``dps``.
    """
    role = role_from_module_groups(module_group_names)
    if role:
        return role
    if hull_class_for_group(hull_group_id) == "Capital":
        return CAPITAL
    return DPS


def attacker_role(hull_group_id, hull_group_name, weapon_type_id=None) -> str:
    """The coarse, hull-based role of an attacker (no module list is ever available).

    Only a dedicated logi hull (by group name) or a capital hull is inferable; everything else
    is ``dps``. ``weapon_type_id`` is accepted for signature symmetry and future refinement but
    a weapon only confirms the dps default, so it does not change the result today.
    """
    name = (hull_group_name or "").strip().lower()
    if name in _LOGI_HULL_GROUPS:
        return LOGI
    if hull_class_for_group(hull_group_id) == "Capital":
        return CAPITAL
    return DPS


def _better(existing: str | None, candidate: str) -> str:
    """The higher-precedence of two roles (lower rank index wins)."""
    if existing is None:
        return candidate
    return existing if _ROLE_RANK[existing] <= _ROLE_RANK[candidate] else candidate


# --------------------------------------------------------------------------- #
#  SDE metadata resolution (bulk, in-service — keeps the callers query-bounded)
# --------------------------------------------------------------------------- #
def _ship_group_meta(type_ids) -> dict[int, tuple[int | None, str]]:
    """``{type_id: (group_id, group_name_lower)}`` for a batch of hulls."""
    from apps.sde.models import SdeType

    wanted = {int(t) for t in type_ids if t}
    if not wanted:
        return {}
    out: dict[int, tuple[int | None, str]] = {}
    for tid, gid, gname in SdeType.objects.filter(type_id__in=wanted).values_list(
        "type_id", "group_id", "group__name"
    ):
        out[tid] = (gid, (gname or "").strip().lower())
    return out


def _item_group_meta(type_ids) -> dict[int, tuple[str, int | None]]:
    """``{type_id: (group_name_lower, category_id)}`` for a batch of fitted items."""
    from apps.sde.models import SdeType

    wanted = {int(t) for t in type_ids if t}
    if not wanted:
        return {}
    out: dict[int, tuple[str, int | None]] = {}
    for tid, gname, cat in SdeType.objects.filter(type_id__in=wanted).values_list(
        "type_id", "group__name", "group__category_id"
    ):
        out[tid] = ((gname or "").strip().lower(), cat)
    return out


# --------------------------------------------------------------------------- #
#  Battle-report composition ("we brought 2 logi vs their 5")
# --------------------------------------------------------------------------- #
def _entity_of(corp_id, char_id) -> tuple[str, int] | None:
    """The side-membership entity for a pilot: its corporation, else its bare character.

    Mirrors ``battle_sides._entity_of`` so a pilot maps to exactly the entity the detected
    sides are keyed on.
    """
    if corp_id:
        return ("corporation", int(corp_id))
    if char_id:
        return ("character", int(char_id))
    return None


def battle_role_composition(report, side_of_entity: dict[tuple[str, int], int]) -> dict[int, list[dict]]:
    """Per-side role counts over the report's pilots, keyed by detected side index.

    ``side_of_entity`` maps a ``(entity_type, entity_id)`` membership key to its side index
    (built from the detected sides). Each distinct pilot is placed on the side of their entity
    and counted once, under a single role:

      * a pilot who DIED in this battle is classified from their victim fit (item-based,
        authoritative);
      * a pilot seen only as an attacker is classified by the hull approximation, taking the
        highest-precedence role across every hull they flew here.

    Returns ``{side_index: [{"role", "label", "count"}, …]}`` — non-zero roles only, ordered
    by :data:`ROLE_ORDER`. Pilots whose entity is on no detected side are skipped.
    """
    from .models import Killmail, KillmailItem, KillmailParticipant

    km_ids = list(report.killmails.values_list("killmail_id", flat=True))
    if not km_ids:
        return {}

    victims = list(
        Killmail.objects.filter(killmail_id__in=km_ids).values(
            "killmail_id", "victim_ship_type_id", "victim_character_id", "victim_corporation_id"
        )
    )
    parts = list(
        KillmailParticipant.objects.filter(killmail_id__in=km_ids).values(
            "killmail_id", "role", "character_id", "corporation_id", "ship_type_id", "weapon_type_id"
        )
    )
    items = list(
        KillmailItem.objects.filter(killmail_id__in=km_ids).values(
            "killmail_id", "item_type_id", "flag"
        )
    )

    # Resolve SDE metadata once for every ship + item type touched.
    ship_ids = {v["victim_ship_type_id"] for v in victims}
    ship_ids |= {p["ship_type_id"] for p in parts if p["ship_type_id"]}
    ship_meta = _ship_group_meta(ship_ids)
    item_meta = _item_group_meta({it["item_type_id"] for it in items})

    # Fitted module groups per victim mail (module racks only, charges excluded).
    mods_by_km: dict[int, set[str]] = defaultdict(set)
    for it in items:
        gname, cat = item_meta.get(it["item_type_id"], ("", None))
        if cat == _CHARGE_CATEGORY or not gname:
            continue
        if slot_bucket(it["flag"]) in _MODULE_SLOTS:
            mods_by_km[it["killmail_id"]].add(gname)

    char_role: dict[int, str] = {}
    char_entity: dict[int, tuple[str, int]] = {}

    # Attacker approximation first — a pilot's victim fit (below) overrides it authoritatively.
    for p in parts:
        if p["role"] != KillmailParticipant.Role.ATTACKER:
            continue
        cid = p["character_id"]
        if not cid:
            continue
        gid, gname = ship_meta.get(p["ship_type_id"], (None, ""))
        role = attacker_role(gid, gname, p["weapon_type_id"])
        char_role[cid] = _better(char_role.get(cid), role)
        ent = _entity_of(p["corporation_id"], p["character_id"])
        if ent is not None:
            char_entity.setdefault(cid, ent)

    # Victims — the item-based, authoritative role for every pilot who died here.
    for v in victims:
        cid = v["victim_character_id"]
        if not cid:
            continue
        gid, _gname = ship_meta.get(v["victim_ship_type_id"], (None, ""))
        char_role[cid] = victim_role(mods_by_km.get(v["killmail_id"], set()), gid)
        ent = _entity_of(v["victim_corporation_id"], cid)
        if ent is not None:
            char_entity[cid] = ent

    tally: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for cid, role in char_role.items():
        ent = char_entity.get(cid)
        side = side_of_entity.get(ent) if ent else None
        if side is None:
            continue
        tally[side][role] += 1

    return {
        side: [
            {"role": role, "label": ROLE_LABELS[role], "count": counts[role]}
            for role in ROLE_ORDER
            if counts.get(role)
        ]
        for side, counts in tally.items()
    }
