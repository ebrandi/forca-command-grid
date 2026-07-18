# Third-party notices

This file records third-party software and data that [FORCA] Command Grid **uses,
references, or evaluated**, together with the attribution their licences require. For the
per-dependency inventory of runtime packages see [`NOTICE.md`](./NOTICE.md) and
[`handbooks/reference/dependency-inventory.md`](./handbooks/reference/dependency-inventory.md);
for community data sources see [`ACKNOWLEDGEMENTS.md`](./ACKNOWLEDGEMENTS.md).

## EVE Online data (CCP hf.)

The ship-fitting engine (Tocha's Lab) and the wider application consume the **CCP EVE
Static Data Export (SDE)**, including its dogma data (`dogmaAttributes`, `dogmaEffects`,
`typeDogma`), imported via `manage.py load_sde` / `manage.py load_dogma`. EVE Online, the
SDE, ESI and EVE imagery are the property of **CCP hf.** and are used under CCP's developer
terms. This project is a non-commercial fan project, not affiliated with or endorsed by CCP.

## EVEShipFit — evaluated, not adopted (MIT)

For the Tocha's Lab fitting engine we performed a documented technical and licence
evaluation of the **EVEShipFit** organisation (https://github.com/EVEShipFit) — repositories
`dogma-engine`, `data`, `sde`, `react`, `eveship.fit`, `preview-renderer`,
`static-data-viewer`, `demonstrator`. All are licensed **MIT**; the EVE *data* they process
remains CCP-owned under the CCP Developer Licence.

**Outcome:** Command Grid built an **independent, server-side Python engine** and did **not**
adopt, fork, vendor, or copy code, tests, fixtures, or generated data from EVEShipFit. No
EVEShipFit source or data is present in this repository. EVEShipFit's `dogma-engine` may be
used by maintainers **only as an external, black-box validation reference** during
development; it is not a build or runtime dependency and is not distributed with this
project.

Because we neither distribute nor copy their code, MIT imposes no notice obligation on the
shipped product. We nonetheless record the attribution here in the interest of transparent
provenance:

> Copyright (c) 2023 EVEShipFit Team — Licensed under the MIT License.

The full evaluation (the 20 mandated questions on maintainability, runtime model, data
provenance, security/supply-chain risk, licence compatibility and abandonment risk) and the
architecture decision are in
[`docs/architecture/decisions/tochas-lab-fitting-engine.md`](./docs/architecture/decisions/tochas-lab-fitting-engine.md)
and summarised in `handbooks/contributor-handbook/decision-log.md`.

### Why an independent engine (summary)

- EVEShipFit's engine is Rust→WebAssembly, browser-first; its native path needs a token-gated
  package (`@eveshipfit/data`) and ships no behavioural tests. FORCA is server-side Django,
  strict-CSP, no JS framework — an architectural mismatch.
- The dogma **data** we need is in the CCP SDE we already import, so no third-party data
  pipeline is required.
- An independent Python implementation keeps the stack pure (no Rust/WASM/registry), keeps
  provenance clean, and lets every mechanic be validated by an independent hand-computed test.
