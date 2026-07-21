"""Serializers for the killboard REST API (KB-28).

Shape is driven from the same helpers the HTML board uses (``fitrender.build_fit`` for the
slot layout, ``anatomy.attacker_breakdown`` for damage shares/parties) so the API and the
page can never drift. Sensitive fields — fit deviation and SRP status — are dropped here
from request context rather than in the view, so a single serializer serves every tier and
the gating lives in one place.

Privacy rule (mission §): never serialise ESI tokens, a member's e-mail/username, or private
fit names. Killmail bodies + our own doctrine tags are already public on the board; the only
tier-gated additions are the deviation diff and the SRP payout/status, both owner/officer.
"""
from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.killboard import anatomy, fitrender


# --------------------------------------------------------------------------- #
#  Name resolution (batched, bounded — used only by the single-object detail)
# --------------------------------------------------------------------------- #
def type_names(type_ids) -> dict[int, str]:
    from apps.sde.models import SdeType

    ids = {t for t in type_ids if t}
    if not ids:
        return {}
    return dict(SdeType.objects.filter(type_id__in=ids).values_list("type_id", "name"))


def _system_name(system_id) -> str | None:
    from apps.sde.models import SdeSolarSystem

    if not system_id:
        return None
    return (
        SdeSolarSystem.objects.filter(system_id=system_id).values_list("name", flat=True).first()
    )


def _region_name(region_id) -> str | None:
    from apps.sde.models import SdeRegion

    if not region_id:
        return None
    return SdeRegion.objects.filter(region_id=region_id).values_list("name", flat=True).first()


def _victim_dict(km) -> dict:
    return {
        "character_id": km.victim_character_id,
        "corporation_id": km.victim_corporation_id,
        "alliance_id": km.victim_alliance_id,
        "faction_id": km.victim_faction_id,
        "ship_type_id": km.victim_ship_type_id,
        "damage_taken": km.damage_taken,
    }


def _doctrine_tag(km) -> dict | None:
    """The loss's doctrine tag ``{fit_id, fit_name, doctrine_id, doctrine_name}`` or None.

    Reads ``km.doctrine_fit`` — the list/detail querysets ``select_related`` it, so this adds
    no query. Doctrine + fit names are corp-authored, already shown on the public board.
    """
    fit = km.doctrine_fit
    if fit is None:
        return None
    doctrine = fit.doctrine
    return {
        "fit_id": fit.id,
        "fit_name": fit.name,
        "doctrine_id": doctrine.id if doctrine else None,
        "doctrine_name": doctrine.name if doctrine else None,
    }


def fit_payload(km, deviation) -> dict:
    """``fitrender.build_fit`` shaped for JSON: slot sections + has_slot_data.

    ``deviation`` MUST be pre-gated by the caller (None for viewers not allowed to see it) —
    ``build_fit`` only marks off-doctrine modules when a deviation is supplied, so a peer
    receives an identical fit with every ``off_doctrine`` false.
    """
    fit = fitrender.build_fit(km, deviation)
    sections = [
        {
            "key": s["key"],
            "label": str(s["label"]),
            "capacity": s["capacity"],
            "filled": s["filled"],
            "count": s["count"],
            "value": s["value"],
            "items": [
                {
                    "type_id": it["type_id"],
                    "name": it["name"],
                    "flag": it["flag"],
                    "destroyed": it["destroyed"],
                    "dropped": it["dropped"],
                    "quantity": it["qty"],
                    "value": it["value"],
                    "off_doctrine": it["off_doctrine"],
                    "empty": it["empty"],
                }
                for it in s["items"]
            ],
        }
        for s in fit["sections"]
    ]
    return {"has_slot_data": fit["has_slot_data"], "sections": sections}


def deviation_payload(deviation) -> dict | None:
    """The fit-deviation diff, or None. Caller gates access; this only shapes it."""
    if deviation is None:
        return None
    return {
        "is_clean": deviation.is_clean,
        "missing": deviation.missing,
        "extra": deviation.extra,
    }


def srp_payload(claim) -> dict | None:
    """The SRP claim's status/payout, or None. Caller gates access; this only shapes it.

    Deliberately scalar-only: the claimant user (and thus any e-mail/username) is never
    serialised — only the status, computed/approved payout, doctrine name and decision notes
    the loss owner and officers already see on the board.
    """
    if claim is None:
        return None
    return {
        "status": claim.status,
        "payout": claim.payout,
        "computed_payout": claim.computed_payout,
        "approved_payout": claim.approved_payout,
        "payout_mode": claim.payout_mode,
        "loss_value": claim.loss_value,
        "auto_drafted": claim.auto_drafted,
        "doctrine": claim.doctrine.name if claim.doctrine_id else None,
        "explanation": claim.explanation,
        "reason": claim.reason,
        "decided_at": claim.decided_at,
    }


# --------------------------------------------------------------------------- #
#  Killmail list
# --------------------------------------------------------------------------- #
class VictimSerializer(serializers.Serializer):
    character_id = serializers.IntegerField(allow_null=True)
    corporation_id = serializers.IntegerField(allow_null=True)
    alliance_id = serializers.IntegerField(allow_null=True)
    faction_id = serializers.IntegerField(allow_null=True)
    ship_type_id = serializers.IntegerField()
    ship_name = serializers.CharField(required=False, allow_null=True)
    damage_taken = serializers.IntegerField()


class DoctrineTagSerializer(serializers.Serializer):
    fit_id = serializers.IntegerField()
    fit_name = serializers.CharField(allow_null=True)
    doctrine_id = serializers.IntegerField(allow_null=True)
    doctrine_name = serializers.CharField(allow_null=True)


class KillmailListSerializer(serializers.Serializer):
    """A lean feed row. Names are NOT resolved here (keeps the list query count constant,
    independent of page size); a client resolves ids via the SDE or the detail endpoint."""

    killmail_id = serializers.IntegerField()
    killmail_hash = serializers.CharField()
    killmail_time = serializers.DateTimeField()
    solar_system_id = serializers.IntegerField()
    region_id = serializers.IntegerField(allow_null=True)
    sec_band = serializers.CharField()
    home_corp_role = serializers.CharField()
    total_value = serializers.DecimalField(max_digits=20, decimal_places=2)
    destroyed_value = serializers.DecimalField(max_digits=20, decimal_places=2)
    dropped_value = serializers.DecimalField(max_digits=20, decimal_places=2)
    points = serializers.IntegerField()
    is_solo = serializers.BooleanField()
    is_npc = serializers.BooleanField()
    is_awox = serializers.BooleanField()
    attacker_count = serializers.IntegerField()
    final_blow_character_id = serializers.SerializerMethodField()
    victim = serializers.SerializerMethodField()
    doctrine = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.INT)
    def get_final_blow_character_id(self, km):
        # ``final_blowers`` is prefetched (role=attacker, final_blow=True) — no query per row.
        blowers = getattr(km, "final_blowers", [])
        return blowers[0].character_id if blowers else None

    @extend_schema_field(VictimSerializer)
    def get_victim(self, km):
        return _victim_dict(km)

    @extend_schema_field(DoctrineTagSerializer)
    def get_doctrine(self, km):
        return _doctrine_tag(km)


# --------------------------------------------------------------------------- #
#  Killmail detail
# --------------------------------------------------------------------------- #
class AttackerSerializer(serializers.Serializer):
    character_id = serializers.IntegerField(allow_null=True)
    corporation_id = serializers.IntegerField(allow_null=True)
    alliance_id = serializers.IntegerField(allow_null=True)
    ship_type_id = serializers.IntegerField(allow_null=True)
    ship_name = serializers.CharField(allow_null=True)
    weapon_type_id = serializers.IntegerField(allow_null=True)
    damage_done = serializers.IntegerField()
    damage_pct = serializers.FloatField()
    final_blow = serializers.BooleanField()
    is_top = serializers.BooleanField()
    doctrine_hull = serializers.BooleanField()


class KillmailDetailSerializer(serializers.Serializer):
    """Full killmail. Assembled in ``to_representation`` from the same helpers the detail
    page uses. The view supplies, via context: ``home_corp_id``, the gated ``deviation`` and
    ``srp`` (None when the viewer isn't the loss owner or an officer)."""

    def to_representation(self, km):
        ctx = self.context
        home_corp_id = ctx.get("home_corp_id") or 0
        deviation = ctx.get("deviation")  # already gated by the view
        srp = ctx.get("srp")              # already gated by the view

        attackers = list(km.participants.filter(role="attacker").order_by("-damage_done"))
        breakdown = anatomy.attacker_breakdown(
            km, attackers, home_corp_id, anatomy.doctrine_hull_ids()
        )
        # Resolve the handful of ship names on the mail (one SDE query).
        names = type_names(
            [km.victim_ship_type_id] + [a.ship_type_id for a in attackers]
        )
        attacker_rows = [
            {**row, "ship_name": names.get(row["ship_type_id"])} for row in breakdown["rows"]
        ]

        victim = _victim_dict(km)
        victim["ship_name"] = names.get(km.victim_ship_type_id)

        return {
            "killmail_id": km.killmail_id,
            "killmail_hash": km.killmail_hash,
            "killmail_time": km.killmail_time,
            "solar_system_id": km.solar_system_id,
            "solar_system_name": _system_name(km.solar_system_id),
            "region_id": km.region_id,
            "region_name": _region_name(km.region_id),
            "sec_band": km.sec_band,
            "home_corp_role": km.home_corp_role,
            "is_solo": km.is_solo,
            "is_npc": km.is_npc,
            "is_awox": km.is_awox,
            "points": km.points,
            "value": {
                "total": km.total_value,
                "destroyed": km.destroyed_value,
                "dropped": km.dropped_value,
                "fitted": km.fitted_value,
                "tier": (lambda t: str(t) if t is not None else None)(
                    anatomy.value_tier(km.total_value)
                ),
            },
            "victim": victim,
            "attackers": AttackerSerializer(attacker_rows, many=True).data,
            "parties": breakdown["parties"],
            "fit": fit_payload(km, deviation),
            "doctrine": _doctrine_tag(km),
            "related_killmail_ids": [k.pk for k in anatomy.related_killmails(km)],
            "battle_report_ids": list(km.battle_reports.values_list("pk", flat=True)),
            # Tier-gated (None unless the view resolved the viewer as owner/officer):
            "deviation": deviation_payload(deviation),
            "srp": srp_payload(srp),
        }
