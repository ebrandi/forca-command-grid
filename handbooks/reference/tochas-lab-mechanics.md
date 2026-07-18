# Tocha's Lab — supported mechanics & data pipeline

The fitting engine (`apps/fitting/engine`) is an independent, server-side dogma evaluator.
It is reached only through the `FittingEngine` adapter boundary and reads all data through a
data provider, so it never touches the ORM, ESI, the request or the network directly. This
page is the living support matrix. See
`docs/architecture/decisions/tochas-lab-fitting-engine.md` for the architecture decision and
`THIRD_PARTY_NOTICES.md` for provenance.

## Correctness policy

Every headline number is verified in `tests/test_fitting_engine.py` against a value computed
**by hand from documented EVE mechanics** — never against another engine's output. A mechanic
that is not modelled is reported in `FittingResult.unsupported` and shown in the UI ("Not
modelled for this fit: …"); it is never silently approximated.

## Supported and tested

| Area | Details |
| --- | --- |
| Fitting resources | CPU, powergrid, calibration (used vs output); high/med/low/rig slot counts; turret & launcher hardpoints; drone bandwidth & bay. Offline modules consume no CPU/PG. |
| Slot & hardpoint limits | Over-slot and over-hardpoint fits are diagnosed and marked *impossible*. |
| Stacking penalty | `S(i) = exp(-(i/2.67)²)`, reproducing EVE's 1.00 / 0.869 / 0.571 / 0.283 / 0.106 / 0.030 table; order-independent. |
| EHP & resists | Shield/armor/hull HP (with flat module HP and %-based skill/ship bonuses), resonance per damage type after stacking-penalised hardeners, EHP weighted by the damage profile. |
| Offence | Turret, **missile** and drone DPS and volley from charge damage × damage multiplier ÷ rate of fire, with ship/role bonuses, skill bonuses (Surgical Strike, Rapid Firing, Warhead Upgrades) and stacking-penalised damage mods of the correct class (gyros→turrets, Ballistic Control→missiles, never cross-boosting); damage-type distribution; missing-ammo diagnostic. |
| Capacitor | Capacity, recharge time, peak recharge (`0.5·C/τ`), module drain, stability (with stable %) or unstable runtime. |
| Mobility | Max velocity (+ Navigation), afterburner/MWD velocity, align time (`ln(4)·mass·agility/1e6`), signature, warp speed; MWD mass/signature penalties. |
| Targeting | Targeting range, locked targets, scan resolution, sensor strength. |
| Skills | A pilot's real skills (from the latest snapshot), All-V, and untrained profiles; missing-skill detection with training-time estimates (reusing `apps.skills`). |
| Profiles | Selectable incoming damage profile and operating mode; the profile in use is recorded in the result. |
| Explainability | Per-attribute contribution traces (base → ship/role/skill/module/stacking → final). |

## Not yet modelled (reported as unsupported)

Missile *application* (explosion radius/velocity vs target signature/speed); turret tracking hit-quality; fighters and fighter tubes; command
bursts / fleet effects; projected and remote effects (reps, cap transfer, EWAR strength);
Tech III subsystem bonuses; overheating effects beyond state handling. Each is surfaced
honestly rather than approximated, and each is a documented extension point:
`apps/fitting/engine/effects.py` (module effects) and `bonuses.py` (ship/skill bonuses).

## Data pipeline

The SDE subset gained dogma reference tables — `SdeDogmaAttribute`, `SdeDogmaEffect`,
`SdeTypeAttribute`, `SdeTypeEffect`, `SdeShipBonus` — populated by:

```
manage.py load_dogma [--file path/to/dogma.json] [--dogma-version YYYYMMDD]
```

The JSON is a relational projection of the CCP SDE FSD dogma files (`dogmaAttributes.yaml`,
`dogmaEffects.yaml`, `typeDogma.yaml`) plus a FORCA-authored `ship_bonuses` projection of hull
traits. The import is idempotent and staged (delete-then-load per touched type inside one
transaction), so a partial run never leaves the engine reading half-updated data. It records
a `dogma_data_version` in `AppSetting`; that version is folded into every calculation's cache
key, so a data refresh transparently invalidates stale results. A hull with no `SdeShipBonus`
rows still evaluates correctly for everything except its hull-specific bonuses (the UI shows
skill-only readiness); populating the full hull catalogue is a staged data task.

Until a full `load_dogma` runs against a complete SDE, only the types present in the loaded
dogma data have attributes; others evaluate from their base slot-count columns only.
