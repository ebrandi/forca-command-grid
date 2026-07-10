"""``doctrine.qualified_pilots`` — corp members who can fly a doctrine (doc 00 §6, doc 02 §4.2).

Reads the cached ``apps.doctrines.services.corp_doctrine_coverage`` (``can_fly`` = optimal +
viable), optionally intersecting the roster with the recently-active set from
``apps.readiness.dimensions.roles.active_member_ids``. Read-only; doctrine data is never mutated.
"""
from __future__ import annotations

from .base import Measurement, MetricSource, _dec, register


class DoctrineQualifiedPilots(MetricSource):
    key = "doctrine.qualified_pilots"
    label = "Doctrine — qualified pilots"
    unit = "pilots"
    data_class = "skills"
    params_schema = [
        {"name": "doctrine_id", "kind": "int", "widget": "doctrine", "label": "Doctrine", "required": True,
         "help": "Active doctrine to count fly-capable pilots for."},
        {"name": "active_days", "kind": "int", "label": "Active within N days", "required": False,
         "help": "Optional — only count pilots seen online in the last N days."},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.db.models import Max
        from django.utils import timezone

        from apps.characters.models import CharacterSkillSnapshot
        from apps.doctrines.services import corp_doctrine_coverage
        from apps.readiness.dimensions.roles import active_member_ids
        from apps.sso.models import EveCharacter

        doctrine_id = int(params["doctrine_id"])
        active_days = params.get("active_days")

        characters = list(EveCharacter.objects.filter(is_corp_member=True))
        if active_days:
            active = active_member_ids(int(active_days))
            characters = [c for c in characters if c.character_id in active]

        can_fly = 0
        for row in corp_doctrine_coverage(characters):
            if row["doctrine_id"] == doctrine_id:
                can_fly = row["can_fly"]
                break

        # Honest freshness: the latest skill snapshots the coverage engine actually read.
        as_of = CharacterSkillSnapshot.objects.filter(is_latest=True).aggregate(
            m=Max("as_of")
        )["m"] or timezone.now()
        detail = {"doctrine_id": doctrine_id, "considered_pilots": len(characters)}
        if active_days:
            detail["active_days"] = int(active_days)
        return Measurement(value=_dec(can_fly), as_of=as_of, detail=detail)


register(DoctrineQualifiedPilots())
