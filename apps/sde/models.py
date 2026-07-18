"""Static Data Export (SDE) reference models.

A pragmatic relational subset of the SDE sufficient for: type/system name
resolution, ship hierarchy, doctrine skill-requirement derivation, and BOM
expansion. Loaded by `manage.py load_sde` from JSON (fixtures/Fuzzwork).
See handbooks/contributor-handbook/domain-model.md §15.
"""
from __future__ import annotations

from django.contrib.postgres.indexes import GinIndex
from django.db import models


class SdeCategory(models.Model):
    category_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=200)

    def __str__(self) -> str:
        return self.name


class SdeGroup(models.Model):
    group_id = models.IntegerField(primary_key=True)
    category = models.ForeignKey(SdeCategory, on_delete=models.CASCADE, related_name="groups")
    name = models.CharField(max_length=200)

    def __str__(self) -> str:
        return self.name


class SdeType(models.Model):
    type_id = models.IntegerField(primary_key=True)
    group = models.ForeignKey(SdeGroup, on_delete=models.CASCADE, related_name="types")
    name = models.CharField(max_length=200, db_index=True)
    volume = models.FloatField(default=0.0)
    # Repackaged volume (m³) — much smaller than assembled ``volume`` for ships and
    # containers. Populated from EVE Ref reference-data; used for freight sizing.
    packaged_volume = models.FloatField(null=True, blank=True)
    base_price = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    # Reprocessing/refine batch size (invTypes.portionSize): a type reprocesses in units of
    # this many, yielding the SdeTypeMaterial quantities per portion. 1 for most items; ~100
    # for ore, 1 for ice batches, etc. Drives the ore-buyback valuation (4.9).
    portion_size = models.PositiveIntegerField(default=1)
    published = models.BooleanField(default=True)
    # Skill training multiplier (dogma attr 275); only set for skill types.
    rank = models.PositiveSmallIntegerField(null=True, blank=True)
    # Training attributes (dogma attrs 180/181): the attribute *type ids* (164=charisma,
    # 165=intelligence, 166=memory, 167=perception, 168=willpower) a skill trains on.
    # Only set for skill types; drives the attribute-aware training-time estimate.
    primary_attribute_id = models.PositiveSmallIntegerField(null=True, blank=True)
    secondary_attribute_id = models.PositiveSmallIntegerField(null=True, blank=True)
    # Fitting slot counts (dogma attrs 14=hiSlots / 13=medSlots / 12=lowSlots /
    # 1137=rigSlots). Only meaningful for ship hulls, and only populated by a full SDE
    # (re)import that reads dogma. Null means "unknown" — the KB-21 fit render then draws
    # the loss's occupied slots without empty-slot outlines. Subsystem slots are not
    # imported (a T3 loss always carries its subsystems, so empty outlines are moot).
    hi_slots = models.PositiveSmallIntegerField(null=True, blank=True)
    med_slots = models.PositiveSmallIntegerField(null=True, blank=True)
    low_slots = models.PositiveSmallIntegerField(null=True, blank=True)
    rig_slots = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        # Trigram GIN so the member-facing autocomplete (search_types/ships/skills, all
        # name__icontains) is index-backed instead of a sequential scan (audit M5).
        indexes = [
            GinIndex(fields=["name"], name="sde_type_name_trgm", opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self) -> str:
        return self.name


class SdeRegion(models.Model):
    region_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SdeConstellation(models.Model):
    constellation_id = models.IntegerField(primary_key=True)
    region = models.ForeignKey(SdeRegion, on_delete=models.CASCADE, related_name="constellations")
    name = models.CharField(max_length=200)

    def __str__(self) -> str:
        return self.name


class SdeSolarSystem(models.Model):
    system_id = models.IntegerField(primary_key=True)
    region = models.ForeignKey(SdeRegion, on_delete=models.CASCADE, related_name="systems")
    constellation = models.ForeignKey(
        SdeConstellation, on_delete=models.SET_NULL, null=True, blank=True, related_name="systems"
    )
    name = models.CharField(max_length=200, db_index=True)
    security = models.FloatField(default=0.0)
    # Galactic coordinates in metres (mapSolarSystems x/y/z). Used to compute
    # light-year distances for jump-freighter routing (see apps/logistics/jumps.py)
    # and the top-down (x, z) projection for region maps (apps/navigation/maps.py).
    # 0/0/0 means "no coordinates loaded" and is excluded from the jump graph.
    x = models.FloatField(default=0.0)
    y = models.FloatField(default=0.0)
    z = models.FloatField(default=0.0)

    class Meta:
        # Trigram GIN for the system autocomplete (search_systems, name__icontains) — M5.
        indexes = [
            GinIndex(fields=["name"], name="sde_system_name_trgm", opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self) -> str:
        return self.name


class SdeSystemJump(models.Model):
    """A stargate connection between two solar systems (mapSolarSystemJumps).

    Stored both ways at import. Powers the region-map edges and a local gate
    adjacency graph.
    """
    from_system_id = models.IntegerField(db_index=True)
    to_system_id = models.IntegerField(db_index=True)

    class Meta:
        unique_together = ("from_system_id", "to_system_id")


class SdeStation(models.Model):
    """An NPC station (public, dockable). Player structures are not in the SDE —
    those are resolved per-pilot via ESI. Used by the freight location picker."""

    station_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=200, db_index=True)
    system_id = models.IntegerField(db_index=True)
    system_name = models.CharField(max_length=200, blank=True)

    class Meta:
        # Trigram GIN for the station autocomplete (search_stations, name__icontains) — M5.
        indexes = [
            GinIndex(fields=["name"], name="sde_station_name_trgm", opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self) -> str:
        return self.name


class SdeCelestial(models.Model):
    """A celestial body inside a solar system — planet, moon or asteroid belt.

    Loaded from the SDE (``mapDenormalize``) so the system page can list named
    planets/moons/belts without a per-request ESI call. Stargates, suns and stations
    are excluded (stations live in ``SdeStation``).
    """

    class Kind(models.TextChoices):
        PLANET = "planet", "Planet"
        MOON = "moon", "Moon"
        BELT = "belt", "Asteroid belt"

    item_id = models.BigIntegerField(primary_key=True)
    system_id = models.IntegerField(db_index=True)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    type_id = models.IntegerField(null=True, blank=True)
    name = models.CharField(max_length=200, blank=True)
    # The planet this moon/belt orbits (null for planets themselves); lets us group
    # moons and belts under their planet on the system page.
    parent_planet_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    celestial_index = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["celestial_index", "name"]

    def __str__(self) -> str:
        return self.name or f"{self.kind} {self.item_id}"


class SdeTypeSkill(models.Model):
    """A skill (and level) required to use a type (derived from dogma attrs)."""

    type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="required_skills")
    skill_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="+")
    level = models.PositiveSmallIntegerField()

    class Meta:
        unique_together = ("type", "skill_type")


class SdeBlueprintMaterial(models.Model):
    """A material input for producing a product through an industry activity.

    ``activity`` is one of ``manufacturing`` / ``reaction`` / ``invention`` —
    the deterministic build tree is manufacturing + reaction; invention rows
    capture the datacores a T2/T3 blueprint copy consumes. ``output_quantity``
    is how many units one run yields (e.g. reactions yield batches, ammo
    manufactures in hundreds), so the BOM can size runs correctly.
    """

    MANUFACTURING = "manufacturing"
    REACTION = "reaction"
    INVENTION = "invention"

    blueprint_type_id = models.IntegerField(db_index=True)
    product_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="+")
    material_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="+")
    quantity = models.BigIntegerField()
    output_quantity = models.BigIntegerField(default=1)
    activity = models.CharField(max_length=32, default=MANUFACTURING)

    class Meta:
        indexes = [models.Index(fields=["product_type", "activity"])]


class SdeTypeMaterial(models.Model):
    """Reprocessing/refining yield: reprocessing one ``portion_size`` batch of ``type`` gives
    ``quantity`` units of ``material_type`` (invTypeMaterials). Ore/ice/scrap reprocess into
    minerals/isotopes; the ore-buyback valuation (4.9) values a batch by its mineral output.
    """

    type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="reprocess_materials")
    material_type_id = models.IntegerField(db_index=True)
    quantity = models.BigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["type", "material_type_id"], name="uniq_type_material"),
        ]


class SdeBlueprintSkill(models.Model):
    """A skill (+ level) required to *run* an industry activity on a blueprint.

    This is the **manufacturing** / **reaction** / **invention** skill requirement —
    distinct from :class:`SdeTypeSkill`, which is "skills to *use* a type" (i.e. to
    fly the ship). Sourced from the SDE ``industryActivitySkills`` table, keyed by the
    product so a "can this pilot build/invent X?" check is a single indexed lookup.
    """

    MANUFACTURING = "manufacturing"
    REACTION = "reaction"
    INVENTION = "invention"

    blueprint_type_id = models.IntegerField(db_index=True)
    product_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="+")
    skill_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="+")
    level = models.PositiveSmallIntegerField(default=1)
    activity = models.CharField(max_length=32, default=MANUFACTURING)

    class Meta:
        indexes = [models.Index(fields=["product_type", "activity"])]
        constraints = [
            models.UniqueConstraint(
                fields=("product_type", "skill_type", "activity"), name="uniq_blueprint_skill"
            ),
        ]


class SdeInventionProduct(models.Model):
    """Invention (industry activity 8) output.

    A T1 blueprint (``t1_blueprint_type_id``) invents a T2/T3 blueprint copy
    (``t2_blueprint_type_id``) that manufactures ``product_type``. Captures the base
    success ``probability`` (0..1, before skills/decryptor) and ``runs`` — how many
    runs the resulting BPC carries per successful invention job. Keyed by the
    manufactured product so "how do I invent X?" is one indexed lookup. This is the
    reference data a T2 invention planner needs; the datacore *materials* live in
    :class:`SdeBlueprintMaterial` (``activity="invention"``) and the invention
    *skills* in :class:`SdeBlueprintSkill` (``activity="invention"``).
    """

    t1_blueprint_type_id = models.IntegerField(db_index=True)
    t2_blueprint_type_id = models.IntegerField(default=0)
    product_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="+")
    runs = models.IntegerField(default=1)
    probability = models.DecimalField(max_digits=6, decimal_places=4, default=0)

    class Meta:
        indexes = [models.Index(fields=["product_type"])]


class SdeDecryptor(models.Model):
    """A decryptor and its invention modifiers.

    Decryptors are optional invention inputs that trade probability against BPC
    runs / ME / TE. The four modifiers are fixed per decryptor (SDE dogma attrs
    1112/1124/1113/1114): ``probability_multiplier`` scales base probability,
    ``run_modifier`` adds runs to the invented BPC, ``me_modifier`` / ``te_modifier``
    adjust the BPC's material / time efficiency.
    """

    type_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=128)
    probability_multiplier = models.DecimalField(max_digits=5, decimal_places=3, default=1)
    run_modifier = models.SmallIntegerField(default=0)
    me_modifier = models.SmallIntegerField(default=0)
    te_modifier = models.SmallIntegerField(default=0)

    def __str__(self) -> str:
        return self.name


class SdeBlueprintActivityTime(models.Model):
    """Base duration (seconds) of an industry activity on a blueprint.

    ``time`` is the SDE base time before ME/TE research, skills, implants and
    structure/rig bonuses — those are applied (or exposed as assumptions) by the
    calculator. Keyed by blueprint + activity.
    """

    MANUFACTURING = "manufacturing"
    COPYING = "copying"
    RESEARCH_ME = "research_me"
    RESEARCH_TE = "research_te"
    INVENTION = "invention"
    REACTION = "reaction"

    blueprint_type_id = models.IntegerField(db_index=True)
    product_type = models.ForeignKey(
        SdeType, on_delete=models.CASCADE, related_name="+", null=True, blank=True
    )
    activity = models.CharField(max_length=16, default=MANUFACTURING)
    time = models.IntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["product_type", "activity"])]
        constraints = [
            models.UniqueConstraint(
                fields=("blueprint_type_id", "activity"), name="uniq_bp_activity_time"
            ),
        ]
