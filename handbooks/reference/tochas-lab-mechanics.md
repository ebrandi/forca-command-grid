# Tocha's Lab — supported mechanics & data pipeline (engine v2)

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
| Capacitor | Capacity, recharge τ, **peak `2.5·C/τ`**, per-module drain from evaluated costs and cycles, cap-booster injection (reload-aware), stability % from the √x-equilibrium quadratic, ODE-integrated depletion time when unstable. |
| Mobility | Evaluated velocity/agility/mass (armor-plate mass included), AB/MWD thrust `(speedFactor/100)·(thrust/mass)`, mass addition and MWD signature bloom, align `ln(4)·mass·agility/1e6`, warp speed, and **warp time** over a requested distance (default 10 AU) via the CCP "Warp Drive Active" accel/cruise/decel model (accel k = warp AU/s, decel `min(k/3, 2)`, drop-out `min(subwarp/2, 100)` m/s using the propulsion-off subwarp speed). |
| Targeting | Range/scan resolution/sensor strength from evaluated attributes (sensor boosters with scripts apply); with a target signature, **lock time** `min(40000/scanRes/asinh(sig)², 30 min)`. |
| Skills | Real pilot snapshots, All-V, untrained; per-level bonuses scale from data (skill-level pre-multiplication); missing-skill detection over all six required-skill slots. |
| Explainability | Stable diagnostic codes with structured params, localised at the presentation layer. |

## Not modelled (reported honestly, never faked)

Fighters and fighter tubes; projected and environmental effects (incoming ewar, remote
assistance, command bursts); smartbombs, mining yield and DoT (breacher-pod) weapons;
booster side-effects. There is no global "operating mode of operation" fit input — the
engine evaluates every module in its own fitted state, so damage/tank output is not gated
by a mode-of-operation selector (a tactical destroyer's *tactical* mode is a supported,
separate mechanic — see above). The full matrix with per-mechanic status lives in
`docs/fitting/tochas-lab-mechanics-matrix.md`.

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
