# Tocha's Lab — supported mechanics & data pipeline (engine v2.1.0)

The fitting engine (`apps/fitting/engine`) is a server-side **generic dogma evaluator**:
it computes every fitted entity's attributes from the imported CCP modifier graph
(`SdeModifier` + per-attribute `stackable`/default flags + per-effect categories) in
three passes — entity construction (character + every skill, hull, modules with
charges, drones, implants), effect collection gated by module state, and lazy
recursive attribute resolution with canonical operator ordering — then derives the
displayed telemetry from the evaluated attributes. It is reached only through the
`FittingEngine` adapter boundary. See
`docs/architecture/decisions/tochas-lab-calculation-engine.md` for the architecture
decision and provenance notes.

**Engine version 2.1.0** is the sole calculation path. The pre-remediation v1 curated
engine has been **removed** — the pyfa differential harness
(`scripts/tochas_lab_differential_pyfa.py`, run under GPL for study/cross-check only)
is now the independent baseline. The version is stamped on every `FitRevision` and folded
into the telemetry cache key, so bumping it invalidates stale cached results on deploy.

## Correctness policy

Golden-fit tests (`tests/test_fitting_golden_*.py`) evaluate real CCP data slices
through the production path and compare against values **derived in the test from the
slice's base attributes plus documented EVE mechanics** — never against the engine's
own output. A mechanic that is not modelled is reported in
`FittingResult.unsupported` and shown in the UI; it is never silently approximated.
`manage.py fitting_data_check` validates the dataset (presence, referential
integrity, unknown modifier funcs/operations, the documented data patches, a live
sample calculation) and exits non-zero on failure.

## Supported and tested

| Area | Details |
| --- | --- |
| Fitting resources | CPU/PG/calibration used vs output — outputs include module and skill multipliers (Reactor Control, Power Diagnostics, Co-Processors, implants) via the graph; slot counts; turret & launcher hardpoints (energy turrets included); rig-size and charge-group/size validation; drone bandwidth and bay validation. Offline modules apply nothing and consume no CPU/PG. |
| Fit legality | Per-group caps — `maxGroupFitted` (fitted), `maxGroupActive` (active/overloaded), `maxGroupOnline` (online); hull fitting restrictions (`canFitShipGroup*`/`canFitShipType*`/`fitsToShipType`, passing if the ship's group **or** type is whitelisted); implant (`implantness`) and booster (`boosterness`) slot conflicts; and T3C subsystems — one per `subSystemSlot` and a complete set (required count = the hull's distinct subsystem slots, **not** the stale `maxSubSystems` attribute). Each fires only when the governing attribute is present and marks the fit structurally impossible. |
| Tactical modes (T3D) | A tactical destroyer's active mode (`FitInput.mode_type_id`; persisted as a `slot="mode"` items entry) is materialised as an always-on entity, and its dogma modifiers flow through the normal pipeline — every CCP mode modifier is a `postDiv` onto the hull (Defense: sig ÷ 1.5, armour resonances ÷ 1.5; Sharpshooter: lock range/sensor ÷ 0.5; Propulsion: agility/speed). A mode is category 7, so it is **not** stacking-exempt and penalises exactly like a module. The mode↔hull link is CCP-linkless — the tie is group "Ship Modifiers" **and** the mode name beginning with the hull name (pyfa's mechanism, study-only); a mode on the wrong hull or a non-T3D is `mode_invalid_for_ship` (structurally impossible) and applies nothing. A T3D with no mode is valid and evaluates bare. The mode is echoed in `telemetry.ship.mode` for the UI. |
| Siege-class modules (Siege / Bastion / Triage / Industrial Core) | Ordinary active modules — their single default effect carries a large real modifier set (Siege 35, Bastion 49, Triage 43, Industrial Core 29), applied through the graph with no special-casing. Verified by hand-derived goldens: Siege mass ×10, immobilisation, ewar/remote-rep resistances; Bastion shield-boost ×1.6, cycle ×0.8, missile RoF ×0.5, immobilisation. |
| Stacking penalty | `S(i) = exp(-(i/2.67)²)` driven by each attribute's `stackable` flag, applied per (attribute, operator) with positive and negative chains penalised separately; ship/charge/skill/implant/subsystem sources are exempt (as in the game data model). |
| EHP & resists | Layer HP and per-type resonance fully graph-evaluated (plates, extenders, trimarks and other rigs, hardeners on the correct layer, Damage Controls, skills, hull bonuses), EHP weighted by the selected damage profile. |
| Active & passive tank | Shield boost / armor repair / hull repair HP/s from evaluated amount ÷ cycle (ancillary charge multipliers included); passive shield regen peak `2.5·shield/τ` and EHP/s. |
| Offence | Turret (hybrid/projectile/**energy**), missile and drone DPS/volley from evaluated attributes — damage mods, weapon rigs, T2 spec skills, hull traits per level, all data-driven; per-weapon optimal/falloff/tracking incl. ammo modifiers; missile velocity/flight time/range; damage-type distribution; bandwidth-gated drone flights. |
| Applied DPS vs a target (turrets, drones, missiles) | With a target profile (signature, velocity, distance; angular derived as `velocity/distance` unless given) the engine reports applied DPS per weapon and a fit `total_applied_dps`. Turrets/sentries: chance-to-hit `CTH = 0.5^((max(0,dist−optimal)/falloff)² + ((angular·optimalSigRadius)/(tracking·sig))²)`, expected multiplier `min(cth,0.01)·3 + max(0,cth−0.01)·((0.01+cth)/2+0.49)` normalised by its perfect-application value (1.01505) so applied ≤ raw. Mobile drones at least as fast as the target apply in full; slower ones/sentries use the turret formula. Missiles keep `min(1, S/Er, ((S/Er)·(Ev/Vt))^DRF)`. A profile too incomplete for a class (no distance for turrets) yields a null applied value with `applied_reason`, and `applied_complete=false` — never a faked number. |
| Sustained DPS (reload) | Per weapon: magazine `floor(floor(capacity/volume)/chargeRate)`, `time_to_empty = shots·cycle`, `reload_s`, and `sustained_dps = magazine_damage/(time_to_empty + reload)` — the long-run rate once reloads are paid. Frequency crystals with `crystalsGetDamaged=0` never deplete (sustained == burst, magazine fields null); `=1` lenses wear out after `floor(rounds·hp/(volatilityDamage·volatilityChance))` shots. Drones carry no magazine, so they sustain fully. Fit total `total_sustained_dps` alongside the untouched burst `total_dps`. |
| Exotic weapons (smartbombs, vorton projectors, breacher pods) | **Smartbombs** — an area pulse (detected by the `empWave` effect); `dps` = Σ module damage ÷ cycle, `range_m` = `empFieldRange`; no charge → sustained == burst; area → `applied_dps == dps` (`applied_note="aoe"`). **Vorton projectors** — fire Condenser Packs; primary-target `dps` = Σ charge damage × the projector's damage multiplier ÷ RoF, with the missile AoE application formula (the explosion attrs live on the module); the arc to secondary targets is reported (`arc_range_m`/`arc_targets`) but not counted. **Breacher pods** — a non-stacking damage-over-time ticking once per second for `dot_duration_s`; per tick `min(flat_tick, pct_tick% × target_total_HP)` (the % arm needs the optional `target_hp` profile input; without it only the flat arm is shown, with `applied_reason="target_hp_unknown"`). The DoT is typeless (excluded from the damage-type mix but counted in `total_dps`) and does **not** stack across launchers — the fit's breacher contribution is the strongest single launcher. |
| Mining yield (ore / ice / gas / mining drones) | An `industry` telemetry section (present only when the fit mines): per-module `yield_per_cycle` (evaluated `miningAmount`), `cycle_s` (evaluated `duration`), `m3_per_hour`, per-`kind` and overall m³/hour subtotals, plus mining-waste `waste_probability`/`waste_volume_multiplier` when the module carries them. Yield and cycle are graph-evaluated, so hull role/skill mining bonuses fold in (a Venture's +100% ore-yield role bonus makes a bare Miner II 30 m³ untrained); a modulated strip miner's loaded **crystal** pre-multiplies `miningAmount` by its `specializationAsteroidYieldMultiplier` (Modulated Strip Miner II 120 × Veldspar T1 1.625 = 195). Kinds are data-driven — gas = the `miningClouds` effect, ice = requires the Ice Harvesting skill, ore = every other mining laser, drone = a mining drone. The wastage roll itself is reported, not simulated. |
| Capacitor | Capacity, recharge τ, **peak `2.5·C/τ`**, per-module drain from evaluated costs and cycles, cap-booster injection (reload-aware), stability % from the √x-equilibrium quadratic, ODE-integrated depletion time when unstable. |
| Mobility | Evaluated velocity/agility/mass (armor-plate mass included), AB/MWD thrust `(speedFactor/100)·(thrust/mass)`, mass addition and MWD signature bloom, align `ln(4)·mass·agility/1e6`, warp speed, and **warp time** over a requested distance (default 10 AU) via the CCP "Warp Drive Active" accel/cruise/decel model (accel k = warp AU/s, decel `min(k/3, 2)`, drop-out `min(subwarp/2, 100)` m/s using the propulsion-off subwarp speed). |
| Targeting | Range/scan resolution/sensor strength from evaluated attributes (sensor boosters with scripts apply); with a target signature, **lock time** `min(40000/scanRes/asinh(sig)², 30 min)`. |
| Projected effects (incoming ewar / neut-nos / remote reps) | Hostile modules projected **onto** the fit (`FitInput.projected`; persisted as `slot="projected"` items entries, quantity-expanded into independent stacking sources). Web, target painter and sensor dampener are synthesised `targetID` postPercent modifiers (their CCP default effects ship empty `modifierInfo`, mirrored from pyfa's handlers) applied through the graph with normal stacking: web `maxVelocity ×(1+speedFactor/100)`, painter `signatureRadius ×1.30`, dampener `maxTargetRange`/`scanResolution` cut (lock time rises). The warp scrambler uses its real graph (`warpScrambleStatus += strength`). Neut/nos add GJ/s drain to the capacitor model (nos modelled as pure drain); remote shield/armor/hull reps add `incoming_rep` HP/s per layer, reported **separately** from your own active tank. Incoming values are scaled by the hull's evaluated resistance for the family (web `stasisWebifierResistance`, painter `targetPainterResistance`, damp `sensorDampenerResistance`, neut/nos `energyWarfareResistance` — a fitted cap battery lowers it —, reps `remoteRepairImpedance`; all default 1.0). Documented simplifications: full strength **at optimal** (range/falloff ignored) and an **unbonused attacker** (module evaluated at base attributes). A projected module with no target effect is flagged `projected_module_inert` (advisory). Listed in `telemetry.projected`; excluded from EFT export, pricing, stock and doctrine promotion. |
| EWAR application (own offensive modules) | The `ewar` telemetry section lists one entry per OUR active offensive-ewar module — ECM jammers, burst jammers, sensor dampeners, target painters, stasis webs, warp scramblers/disruptors and tracking/guidance disruptors (energy neutralisers/nosferatus are cross-linked from the capacitor section) — each classified by its **default (identifying) effect id** (robust across metalevels; the old group-id readout had mislabelled painters and weapon disruptors into dead code). Each entry carries the module's evaluated strength attribute(s) (post-skills/overload), optimal, falloff, cycle and cap/cycle. **ECM jam chance**: per jammer per sensor type `min(1, scanXStrengthBonus(238-241) / target sensor strength)`, and a **combined** chance across jammers as independent per-cycle rolls `1 − Π(1−p_i)` (pyfa `jamChance` semantics, study-only) — supplied via the target profile's `target_sensor_strength` (+ optional `target_sensor_type`); absent → null with a reason, never faked. **Adjusted target** (`ewar.ewar_on_target`): our painters enlarge the target's signature and our webs slow it, using the SAME stacking-penalised postPercent maths a fitted/projected modifier uses (`graph._calculate`); damps report their lock-range/scan-res deltas (no target base to apply to). **Applied-DPS decision — folded in**: applied DPS is computed against the ewar-adjusted target (physically what the game does when you paint/web before shooting), with the raw-profile totals kept in parallel as `offence.total_applied_dps_unassisted` so both surfaces stay explicit. Tracking/guidance disruptors are **readout-only** — their scripted strengths are surfaced, but our own turret/missile output is not self-disrupted (that would require modelling the enemy shooting us). Lock time stays on the raw signature (a documented scope choice: painting speeds a real lock too, but the applied-DPS decision is scoped to damage). |
| Fleet boosts (friendly command bursts) | Warfare buffs from friendly command bursts boosting the fit (`FitInput.boosts`; persisted as `slot="boost"` items entries carrying the burst **charge** type id + an optional `strength_pct` override). The per-ally buff is **not** dogma (the burst module's default effect has zero modifiers), so the semantics come from CCP's `dbuffCollections.yaml`, imported into `SdeDbuff`/`SdeDbuffModifier` by `import_dogma_graph`. Each charge names a buff id (`warfareBuff1ID` 2468) + multiplier (`warfareBuff1Multiplier` 2596); the **default strength is the multiplier** (the effect of an *unbonused* T1 burst whose base warfareBuffValue is 1.0 — a documented simplification, `strength_pct` overrides it for a real command ship). Applied via the buff's operator (PostPercent/PostMul/ModAdd/Pre/PostAssignment) onto the attributes its dbuff modifiers name, on the resolved targets (`item` → the ship; `location`/`locationGroup`/`locationRequiredSkill` → fitted modules, by group / by required skill). Several boosts of the same buff id do **not** sum — the strongest single instance wins (aggregateMode Maximum → max, Minimum → min). **Stacking**: a boost is a normal penalisable source, so the penalty falls out of the target attribute's `stackable` flag exactly like a fitted module bonus (a shield-resistance buff is penalised and shares the chain with local hardeners; an HP buff is not) — verified against pyfa. A charge referencing a buff id absent from the table → `boost_unknown_buff` (advisory). Listed in `telemetry.boosts`; excluded from EFT export, pricing, stock and doctrine promotion. |
| Mutated (abyssal) modules | A module's rolled attributes are carried as overrides on the fitted item (`ModuleInput.attr_overrides` — `{attribute_id: value}`, persisted as an `attr_overrides` map on the items entry, bounded to 32 entries). Each override **replaces** the provider's base value for that attribute before graph evaluation, and **adds** it when the base type has none — which is the abyssal case: the fittable "Abyssal *X*" SdeType stores only structural attrs (mass/volume/skill), so its damageMultiplier / speedMultiplier / etc. live entirely in the override. Everything downstream (the module's dogma effects, stacking penalty on the target attribute, validations, telemetry) then flows through the normal pipeline with no special-casing — a mutated gyro's overridden damageMultiplier shares the same penalised chain a normal gyro's does. An abyssal-type module fitted **without** overrides evaluates at base-roll (its combat attrs default to 1.0, i.e. it does nothing) and raises `mutated_attributes_unknown` (advisory WARNING — the fit stays valid) so the placeholder numbers are visible, never silently wrong. A non-abyssal type carrying overrides (a pyfa-style base-item + roll) is tolerated and silent. **EFT interchange** uses pyfa's mutation-block syntax — a `[N]` reference on the rack line and, after the racks, `[N] <base item>` / `<mutaplasmid>` / `<attrName value>, …`; FORCA→FORCA round-trips every override exactly (identical `FitInput.hash`) and pyfa→FORCA preserves the base item + overrides (see the round-trip note below). Killmails never carry mutated attributes (ESI omits them), so an imported abyssal loss legitimately trips the warning. |
| Fighters (carrier / supercarrier squadrons) | Fighter squadrons via `FitInput.fighters` (`FighterInput{type_id, count}`; persisted as `slot="fighter"` items entries with `quantity=count`) — a real, priced/stocked/EFT-exported part of the fit. Each squadron is materialised as an active graph entity **exactly like a drone** and joined to the ship's "located" set, so every fighter-damage bonus reaches it through the ordinary `OwnerRequiredSkillModifier` pipeline with no fighter-specific modifier code: all fighter damage/range/RoF modifier rows in the SDE filter on a skill the fighter *requires* (e.g. Fighters 23069). Standard-attack squadron DPS = Σ(evaluated damage 2227-2230) × multiplier(2226) × count ÷ (duration 2233/1000); the multiplier already carries Fighters (+5%/lvl), Drone Interfacing (+10%/lvl — it boosts fighters too), racial Fighter Specialization (+2%/lvl), Heavy Fighters (heavy only), and the **carrier hull damage trait** (e.g. the Nidhoggur's `shipBonusCarrierM1FighterDamage` postPercent by `shipBonusCarrierM1`, pre-scaled by the racial Carrier skill level — the Archon has **no** fighter-damage trait, so the proof uses the Nidhoggur). All are stacking-exempt skill/ship sources. `fighter_dps` folds into `total_dps`/`volley`/`damage_distribution`. Structural validations (all fit-impossible): `fighter_on_non_carrier`, `fighter_tubes_exceeded`, `fighter_role_slots_exceeded` (role by group; structure-fighter groups → 0 ship slots), `fighter_squadron_oversized`, `fighter_bay_exceeded`, `fighter_invalid_type` (placeholder scaffold rows). Telemetry `fighters` section lists per-squadron DPS/role/count/bay and fit totals. EFT renders a squadron as `Templar II x6`. |
| Skills | Real pilot snapshots, All-V, untrained; per-level bonuses scale from data (skill-level pre-multiplication); missing-skill detection over all six required-skill slots. |
| Explainability | Stable diagnostic codes with structured params, localised at the presentation layer. |

## Not modelled (reported honestly, never faked)

**Projected ECM onto us** (a hostile jammer breaking *our* lock is
chance-based — detected, but we do not simulate our own lock dropping as a stat change; our OWN
offensive ECM jam chance *against a target* **is** modelled — see "EWAR application"); booster
side-effects; a vorton projector's arc to secondary targets (only its primary-target hit is
counted); the mining-waste roll (reported as a probability, not simulated as a deterministic
loss). **Fighter** gaps (the squadrons themselves *are* modelled — see "Fighters" above): their
**applied** DPS (fighters carry their own tracking/explosion attributes — every squadron reports
`applied_dps=null` with a reason and a target-set carrier fit is flagged `applied_complete=false`,
never faked); the special long-range missile volley and the utility abilities (web / neut / MWD /
kamikaze), which are separately-toggled and off by default (only the standard attack is counted);
and fighter **rearm** timing for sustained DPS (the NUM_SHOTS / rearm-time data is absent from
CCP's SDE — pyfa hardcodes it — so squadrons are reported as sustaining at their un-rearmed DPS).

**Overheat burnout / heat damage over time.** When a module is set to overheat its *output*
bonuses **are** modelled: the `OVERHEATED` state maps to the overloaded graph state and every
category-5 overload effect (`overloadRofBonus` 1205, `overloadDamageModifier` 1210, the overload
tank/range/repair bonuses, …) applies generically through the modifier graph, exactly like any
other effect — nothing weapon-specific is hard-coded. What is **not** modelled is the *cost* of
overheating: the per-cycle heat damage a module deals to itself, its spread across the rack
(attenuation-decayed by slot distance), and therefore any "time to burnout" estimate. That
mechanic is **stochastic** — each overloaded cycle carries a damage *chance*, and propagation
depends on rack heat and a distance attenuation whose equations CCP has never published as a
citable specification; they remain community-reverse-engineered and approximate. The reference
engine **pyfa does not model burnout time either** — its `eos` applies the overload bonuses and
carries the `heatDamage` attribute (modified by Thermodynamics / heat-reduction subsystems), but
simulates no rack-heat propagation or time-to-failure. Building an "expected burnout time" would
mean **inventing** the per-cycle damage probability and the attenuation-decay constants, which
this engine refuses to do (numbers are never faked). The SDE heat attributes are present
(`heatDamage` 1211, `heatCapacityHi/Med/Low` 1178/1199/1200, `heatDissipationRate*` 1179/1196/1198,
`heatAttenuation*` 1259/1261/1262, `heatAbsorbtionRate*` 1182-1184, `heatGenerationMultiplier`
1224, `moduleRepairRate` 1267) should a citable specification later make a deterministic
expected-value model possible.

(Smartbombs, vorton projectors, breacher-pod DoT and mining yield *are* now modelled —
see "Exotic weapons" and "Mining yield" above; fleet command bursts, incoming ewar, neut/nos
pressure and remote reps *are* now modelled — see "Fleet boosts" and "Projected effects" above;
our own offensive EWAR — jam chance, painter/web-adjusted application, damp/TD/GD readouts — is
now modelled, see "EWAR application".) **Environmental / abyssal-weather effects
are also warfare buffs** (the same `dbuffCollections` machinery — e.g. buffs 79-93 are AOE
beacon / weather effects), so the *application* path exists, but there is no environment
**selection** input wired to it yet: a future workstream would let a fit pick a weather/abyssal
context. Until then environment effects are not applied (never faked). There is no global
"operating mode of operation" fit input — the
engine evaluates every module in its own fitted state, so damage/tank output is not gated
by a mode-of-operation selector (a tactical destroyer's *tactical* mode is a supported,
separate mechanic — see above). The full matrix with per-mechanic status lives in
`docs/fitting/tochas-lab-mechanics-matrix.md`.

## Mutated-module EFT format

Tocha's Lab reads and writes pyfa's mutation-block syntax (studied under GPL, implemented
independently). A mutated module keeps its normal rack line with a trailing `[N]` reference,
and each mutant is described by a three-line block appended after all racks:

```
[Rifter, Abyssal example]

Gyrostabilizer II [1]

[1] Gyrostabilizer II
  Unstable Gyrostabilizer Mutaplasmid
  damageMultiplier 1.35, speedMultiplier 0.8
```

The block is `[N] <base item>` / `<mutaplasmid name>` / `<attrName value>, …` (attributes
sorted by name). On import the block is lifted out first, the base-item and mutaplasmid lines
are read but only the attribute line is kept (as `attr_overrides`), and an unresolvable
attribute name is surfaced in the import's `unresolved` list like an unresolvable module name.

**Round-trip fidelity** (FORCA models a mutation as attribute overrides only — it does **not**
track mutaplasmid identity):

- **FORCA → FORCA**: lossless. Every override is preserved and the reconstructed fit has an
  identical `FitInput.hash`. The emitted mutaplasmid line is a fixed placeholder
  (`Unknown Mutaplasmid`).
- **pyfa → FORCA**: the base item and its overridden attributes are preserved; the mutaplasmid
  identity is dropped (not modelled). pyfa expresses a mutation as base source item + roll,
  which maps directly onto `attr_overrides`.
- **FORCA → pyfa**: the overrides are **lost** — pyfa needs a real mutaplasmid name on the
  middle line to keep the roll, and FORCA does not have one to emit. The module still imports
  into pyfa as a plain (unmutated) item. This is the one documented lossy direction.

## Data pipeline

Two coordinated imports populate the dogma layer:

```
manage.py import_sde_fuzzwork              # types/groups/attribute defs/type dogma (Fuzzwork, daily)
manage.py import_dogma_graph               # modifier graph + ship traits + skill dogma (CCP official SDE)
manage.py fitting_data_check               # deploy gate — non-zero exit on critical failure
```

`import_dogma_graph` reads the **current** official distribution at
developers.eveonline.com/static-data (the legacy S3 `sde.zip` was frozen in July
2025) and records the CCP build number as the data version. It also synthesises the
six documented client-internal effects (missile damage skills, `selfRof`, Drone
Interfacing) that CCP ships with empty `modifierInfo`. Fuzzwork's import synthesises
mass/capacity/volume attributes from `invTypes` and imports all six required-skill
slots. **Order matters**: a full Fuzzwork run cascade-clears the graph tables —
always run `import_dogma_graph` after it; `fitting_data_check` fails loudly on the
in-between state. Every data version is folded into the calculation cache key, so a
refresh transparently invalidates stale results.
