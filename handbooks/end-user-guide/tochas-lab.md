# Tocha's Lab — the fitting workspace

*Laboratório do Tocha* (pt-BR). Tocha's Lab lets you build and simulate ship fits, apply your
real skills, price and stock-check them, compare versions, share them, and turn the best into
corp doctrines. Open it from **Ships & Doctrines → Tocha's Lab**, or go to `/lab/`.

## Create your first fit

1. On the Tocha's Lab home page, type a ship hull (e.g. *Rifter*) and select **Create fit** —
   or paste an EVE-client **EFT** loadout into the import box and select **Import**.
2. You land in the workspace. The left column is the loadout; the right column is the
   live telemetry.
3. Search for a module in the box at the top of the loadout and click a result to add it.
   Switch a module between **on / off / heat**, or remove it with ✕. The telemetry on the
   right updates as you change the fit.
4. Select **Save** to store a revision. Saving never changes an earlier revision — the
   history is kept so a shared or doctrine fit can never change under anyone.

Everything works without the mouse-only conveniences: importing, saving, forking, sharing,
exporting and comparing are ordinary buttons and forms.

## Apply your skills

Use the **skills** selector to simulate with:

- **My skills** — your active pilot's trained skills (the default),
- **All V** — every skill at level V (the theoretical ceiling), or
- **Untrained** — nothing trained (the floor),
- or any of your **linked pilots**.

The **Pilot readiness** panel says whether the selected pilot can fly the fit, lists the
skills still missing (current level → required level) and an estimated training time. The
same skill and training-time logic powers the doctrine pages, so the numbers agree.

## Read the telemetry

The panels group the numbers the way you think about a fit: **Fitting** (CPU/PG/calibration,
slots, hardpoints), **Offence** (DPS, volley, damage split), **Defence** (EHP and the resist
grid per layer), **Capacitor** (stable % or runtime), **Mobility** (velocity, align,
signature) and **Targeting**. See
[reference/tochas-lab-mechanics.md](../reference/tochas-lab-mechanics.md) for exactly what is
and isn't modelled — anything not modelled for your fit is stated plainly, never faked.

## Understand the diagnostics

The **Fit diagnostics** list flags real problems — CPU/PG/calibration exceeded, too many
turrets for the hardpoints, a weapon with no ammo, and so on — each with what was detected,
why it matters and a suggested action. There is deliberately no single "fit score": whether a
trade-off is good depends on what you intend to fly.

## Cost, stock and next actions

**Cost & supply** shows the estimated Jita price (with an "as of" timestamp; unpriced items
are called out rather than counted as free) and whether every component is available from corp
stock, listing what is short.

## Compare, share and export

- **Compare** shows the module and telemetry differences between two revisions, colour-coded
  for the metric (higher DPS/EHP is good; higher align time or signature is not).
- **Create public link** mints an unguessable, read-only share link (shown at All-V skills);
  **Revoke link** turns it off immediately.
- **Export EFT** downloads the fit in the standard EVE-client format.
- **Fork** copies any fit you can see into a new private fit you own, keeping the lineage.

## Turn a fit into a doctrine (officers)

If you are an officer, the **Doctrine candidate** panel lets you publish the current revision
into a chosen doctrine. This is deliberate and audited — saving a fit never publishes a
doctrine. Publishing derives the fit's skill requirements and makes it available to the
readiness and Shipyard workflows.

## Training and buying what you're missing

Missing skills link into the existing skill-planning tools; missing components surface in the
cost & supply panel and route to the corp's normal supply, industry and market workflows —
Tocha's Lab reuses those systems rather than creating parallel ones.
