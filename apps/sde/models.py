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
    # Assembled mass (kg) from invTypes.mass — dogma attribute 4 is NOT in the Fuzzwork dogma
    # export, so the fitting engine bridges this column in when attr 4 is missing (needed for
    # align time and the mass-dependent MWD/AB velocity formula). Null means "unknown".
    mass = models.FloatField(null=True, blank=True)
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


# ---------------------------------------------------------------------------
# Dogma reference data (KB-Tocha) — the attribute/effect layer that the ship
# fitting simulation (Tocha's Lab, apps/fitting) evaluates. This is a faithful
# relational subset of the CCP SDE dogma tables (dgmAttributeTypes / dgmEffects /
# dgmTypeAttributes / dgmTypeEffects), loaded by ``manage.py load_dogma`` from the
# same authoritative Static Data Export the rest of the SDE comes from. It is NOT
# derived from any third-party fitting engine's generated data; see
# docs/architecture/decisions/tochas-lab-fitting-engine.md for provenance.
# ---------------------------------------------------------------------------


class SdeDogmaAttribute(models.Model):
    """A dogma attribute definition (dgmAttributeTypes).

    ``stackable`` False means the attribute's modifiers suffer the stacking penalty
    (resistances, tracking bonuses, etc.); True means they combine at full strength
    (capacitor, capacity, …). ``high_is_good`` records the natural "better" direction
    so comparisons can colour a change correctly (None when it is context-dependent).
    """

    attribute_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=200, db_index=True)
    display_name = models.CharField(max_length=200, blank=True)
    unit_id = models.IntegerField(null=True, blank=True)
    stackable = models.BooleanField(default=True)
    high_is_good = models.BooleanField(null=True, blank=True)
    default_value = models.FloatField(default=0.0)
    published = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.display_name or self.name


class SdeDogmaEffect(models.Model):
    """A dogma effect definition (dgmEffects) plus its modifier graph.

    ``effect_category`` is the SDE effect category (0 passive, 1 active, 2 target,
    3 area, 4 online, 5 overload, …); it decides when an effect contributes (a passive
    resist bonus always applies; an active hardener only when the module is running).
    ``modifier_info`` carries the SDE ``modifierInfo`` list verbatim so a future full
    dogma evaluator can consume it; the current evaluator uses the structured fields
    it needs plus a documented handler set (see apps/fitting/engine/effects.py).
    """

    effect_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=200, db_index=True)
    effect_category = models.IntegerField(default=0)
    is_offensive = models.BooleanField(default=False)
    is_assistance = models.BooleanField(default=False)
    discharge_attribute_id = models.IntegerField(null=True, blank=True)
    duration_attribute_id = models.IntegerField(null=True, blank=True)
    range_attribute_id = models.IntegerField(null=True, blank=True)
    falloff_attribute_id = models.IntegerField(null=True, blank=True)
    tracking_attribute_id = models.IntegerField(null=True, blank=True)
    modifier_info = models.JSONField(default=list, blank=True)

    def __str__(self) -> str:
        return self.name


class SdeModifier(models.Model):
    """One modifier from an effect's SDE ``modifierInfo`` graph (Tocha's Lab dogma engine).

    Fuzzwork's export carries *which* effects a type has (``SdeTypeEffect``) but never *what
    they do*; the "what" lives in CCP's FSD ``dogmaEffects.yaml`` as each effect's
    ``modifierInfo`` list. This table is the normalised form of that list — one row per
    modifier — so the fitting engine can apply every module / skill / charge / ship /
    subsystem effect generically from data instead of a hand-coded handler per class.

    A modifier reads: "change attribute ``modified_attribute_id`` on the targets selected by
    ``func`` by the value of attribute ``modifying_attribute_id`` (read off the source item /
    skill), using ``operation``" (-1 preAssign · 0 preMul · 1 preDiv · 2 modAdd · 3 modSub ·
    4 postMul · 5 postDiv · 6 postPercent · 7 postAssign · 9 skill-level bookkeeping —
    verified against live data: e.g. effect 146 pre-MULTIPLIES attr 292 by 280, operation 0).
    ``func`` is one of ``ItemModifier`` /
    ``LocationModifier`` / ``LocationGroupModifier`` / ``LocationRequiredSkillModifier`` /
    ``OwnerRequiredSkillModifier``; ``group_id`` scopes a ``LocationGroupModifier`` and
    ``skill_type_id`` scopes the RequiredSkill funcs. ``effect_id`` is a plain indexed integer
    (not a FK) — deliberately, to mirror ``SdeTypeEffect.effect_id`` and to survive a Fuzzwork
    effects re-import (which fully replaces ``SdeDogmaEffect`` but preserves effect ids) without
    a cascade wiping the graph or an FK-integrity failure on an effect we happen not to carry.

    Populated by ``manage.py import_dogma_graph`` (a full replace) from the same authoritative
    CCP SDE the hull bonuses come from; nothing here derives from a third-party fitting engine.
    """

    effect_id = models.IntegerField()
    func = models.CharField(max_length=64)
    modified_attribute_id = models.IntegerField(null=True, blank=True)
    modifying_attribute_id = models.IntegerField(null=True, blank=True)
    operation = models.SmallIntegerField(null=True, blank=True)
    group_id = models.IntegerField(null=True, blank=True)
    skill_type_id = models.IntegerField(null=True, blank=True)
    # The modifier's SDE domain ("shipID" / "charID" / "targetID" / "otherID" / …) — kept
    # verbatim so the applicator can distinguish e.g. a charge-domain missile-damage modifier.
    domain = models.CharField(max_length=32, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["effect_id"]),
            models.Index(fields=["modified_attribute_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.effect_id}:{self.func}:{self.modified_attribute_id}"


class SdeDbuff(models.Model):
    """A warfare-buff definition from CCP's ``dbuffCollections.yaml`` (Tocha's Lab WS-7).

    Fleet command bursts (and Titan/environment effects) don't carry their per-ally buff in
    dogma: a burst charge names a ``warfareBuffNID`` (dogma attrs 2468/2470/2472/2536) and a
    ``warfareBuffNMultiplier`` (2596-2599); the burst module's default effect
    (``moduleBonusWarfareLink*``) has ZERO modifiers, because CCP applies the buff to fleet
    members OUTSIDE dogma, keyed by that buff id. ``dbuffCollections.yaml`` is the table that
    says WHAT each buff id does — which attributes it changes, on which items, with which
    operator, and how multiple instances aggregate. This model is FORCA's normalised form of
    that file, imported by ``import_dogma_graph`` alongside the modifier graph.

    ``aggregate_mode`` (``Maximum`` / ``Minimum``) picks the winning value when several boosts
    grant the same buff id — the strongest single instance wins (bursts do NOT sum). The
    stacking penalty of an applied buff is NOT stored here: it is governed entirely by the
    TARGET attribute's ``stackable`` flag, exactly like any module bonus (verified against
    pyfa — every warfare-buff penalty choice matches the attribute's ``stackable`` flag).
    """

    buff_id = models.IntegerField(primary_key=True)
    # CCP aggregateMode: how instances of the same buff id combine (Maximum | Minimum).
    aggregate_mode = models.CharField(max_length=16)
    # CCP operationName mapped to a dogma operator at apply time: PostPercent | PostMul |
    # ModAdd | PostAssignment | PreAssignment.
    operation = models.CharField(max_length=24)
    # developerDescription (English, for the audit trail / telemetry label).
    name = models.CharField(max_length=200, blank=True)

    def __str__(self) -> str:
        return f"{self.buff_id}:{self.name}"


class SdeDbuffModifier(models.Model):
    """One modifier of a :class:`SdeDbuff` — which attribute the buff changes, on what.

    Mirrors the four modifier lists in ``dbuffCollections.yaml``: ``kind`` records which list
    it came from (``item`` → the boosted ship itself; ``location`` → the ship and everything
    on it; ``locationGroup`` → located items of ``group_id``; ``locationRequiredSkill`` →
    located items that require ``skill_type_id``). The buff's ``operation`` and aggregated
    value are applied onto ``modified_attribute_id`` on each resolved target.
    """

    KIND_ITEM = "item"
    KIND_LOCATION = "location"
    KIND_LOCATION_GROUP = "locationGroup"
    KIND_LOCATION_REQUIRED_SKILL = "locationRequiredSkill"

    buff = models.ForeignKey(SdeDbuff, on_delete=models.CASCADE, related_name="modifiers")
    kind = models.CharField(max_length=24)
    modified_attribute_id = models.IntegerField()
    group_id = models.IntegerField(null=True, blank=True)
    skill_type_id = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["buff"])]

    def __str__(self) -> str:
        return f"{self.buff_id}:{self.kind}:{self.modified_attribute_id}"


class SdeTypeAttribute(models.Model):
    """The value of one dogma attribute on one type (dgmTypeAttributes).

    This is the base (pre-fit, pre-skill) value; the fitting engine layers ship, role,
    skill, module, rig and environment modifiers on top. Loaded only for the types the
    feature needs (ships, modules, charges, drones, skills), keeping the table bounded.
    """

    type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="dogma_attributes")
    attribute_id = models.IntegerField()
    value = models.FloatField(default=0.0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["type", "attribute_id"], name="uniq_type_attribute"),
        ]
        indexes = [models.Index(fields=["attribute_id"])]


class SdeTypeEffect(models.Model):
    """An effect present on a type (dgmTypeEffects). ``is_default`` marks the default
    effect (the one a module runs when simply activated)."""

    type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="dogma_effects")
    effect_id = models.IntegerField()
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["type", "effect_id"], name="uniq_type_effect"),
        ]
        indexes = [models.Index(fields=["effect_id"])]


class SdeShipBonus(models.Model):
    """A structured ship / role bonus for a hull (the Tocha's Lab fitting engine input).

    EVE hull bonuses ("+5% Small Projectile damage per level of Minmatar Frigate") are
    expressed in the SDE as dogma effects with a modifier graph and, in the client, as
    trait text. This table is FORCA's normalised form of that bonus: which attribute it
    changes, on which fitted items (by group/category), by how much, and which skill (if
    any) scales it. ``load_dogma`` populates it; the engine turns each row into a
    :class:`apps.fitting.engine.bonuses.BonusSpec`. Absence of rows for a hull means the
    engine applies skill bonuses only (an honest partial, surfaced in the UI).
    """

    ship_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="ship_bonuses")
    key = models.CharField(max_length=64)
    target_attribute_id = models.IntegerField()
    amount = models.FloatField(default=0.0)          # percent (per level if per_level)
    per_level = models.BooleanField(default=False)
    skill_type_id = models.IntegerField(null=True, blank=True)  # None => role bonus (always on)
    target_domain = models.CharField(max_length=8, default="item")  # "item" | "ship" | "charge"
    match_group_ids = models.JSONField(default=list, blank=True)
    match_category_ids = models.JSONField(default=list, blank=True)
    match_attr_present = models.IntegerField(null=True, blank=True)
    # Item/charge must REQUIRE this skill (how EVE scopes most turret/missile hull bonuses).
    match_required_skill_id = models.IntegerField(null=True, blank=True)
    penalised = models.BooleanField(default=False)
    label = models.CharField(max_length=128, blank=True)

    class Meta:
        indexes = [models.Index(fields=["ship_type"])]
        constraints = [
            models.UniqueConstraint(fields=["ship_type", "key"], name="uniq_ship_bonus_key"),
        ]

    def __str__(self) -> str:
        return f"{self.ship_type_id}:{self.key}"
