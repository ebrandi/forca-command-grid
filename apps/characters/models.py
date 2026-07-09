"""Character Profiles: skill / skillqueue / attributes snapshots."""
from __future__ import annotations

from django.db import models

from apps.sso.models import EveCharacter
from core.mixins import ProvenanceMixin


class CharacterSkillSnapshot(ProvenanceMixin):
    """Point-in-time copy of a character's trained skills.

    `skills` maps skill_type_id -> {"trained_level": int, "sp": int}.
    """

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="skill_snapshots"
    )
    skills = models.JSONField(default=dict)
    total_sp = models.BigIntegerField(default=0)
    is_latest = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["character", "is_latest"])]

    def trained_level(self, skill_type_id: int) -> int:
        entry = self.skills.get(str(skill_type_id)) or self.skills.get(skill_type_id)
        return int(entry.get("trained_level", 0)) if entry else 0


class SkillQueueSnapshot(ProvenanceMixin):
    """Current training queue: ordered list of
    {skill_type_id, finish_level, start, finish, sp}."""

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="skillqueue_snapshots"
    )
    entries = models.JSONField(default=list)
    is_latest = models.BooleanField(default=True)


class CharacterAttributes(ProvenanceMixin):
    character = models.OneToOneField(
        EveCharacter, on_delete=models.CASCADE, related_name="attributes"
    )
    intelligence = models.IntegerField(default=20)
    memory = models.IntegerField(default=20)
    perception = models.IntegerField(default=20)
    willpower = models.IntegerField(default=20)
    charisma = models.IntegerField(default=19)
    implants = models.JSONField(default=list, blank=True)


class CharacterFittedShip(ProvenanceMixin):
    """A ship a character owns that has modules fitted, captured from ESI assets.

    ESI assets carry a per-item ``location_flag`` (HiSlot/MedSlot/LoSlot/RigSlot/
    SubSystemSlot) whose ``location_id`` is the ship's item id — so we can reconstruct
    what's actually fitted to each hull (vs sitting loose in a hangar) and compare it to
    the doctrine fit. ``modules`` maps ``str(type_id) -> count`` of fitted modules.
    """

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="fitted_ships"
    )
    item_id = models.BigIntegerField()
    ship_type_id = models.IntegerField(db_index=True)
    location_id = models.BigIntegerField(null=True, blank=True)
    modules = models.JSONField(default=dict)
    is_latest = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["character", "is_latest"])]
