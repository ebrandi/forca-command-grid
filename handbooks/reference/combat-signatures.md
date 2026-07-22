# Combat Signatures Architecture

A contributor's map of the Combat Signatures feature: how a pilot-authored banner goes from a
saved configuration to a PNG on disk that nginx serves, and where to extend it. For the
pilot- and leadership-facing behaviour see the
[Feature catalog](../feature-catalog.md#combat-signatures),
[end-user guide](../end-user-guide/combat-and-progression.md#combat-signatures) and
[Leadership Features](../administrator-handbook/leadership-features.md#combat-signatures); for
the operator view see the
[Operations Runbook](../operator-handbook/operations-runbook.md#combat-signatures).

## Table of contents

- [Module map](#module-map)
- [Data model](#data-model)
- [Render lifecycle](#render-lifecycle)
- [Components, layouts, and slots](#components-layouts-and-slots)
- [Adding a component](#adding-a-component)
- [Adding a layout](#adding-a-layout)
- [Adding a background](#adding-a-background)
- [Image-testing conventions](#image-testing-conventions)
- [The translation trap](#the-translation-trap)

## Module map

The feature is a bounded context inside `apps/killboard`, one module per concern:

| Module | Responsibility |
|---|---|
| `signatures.py` | Domain layer: the strict config schema + `validate_config`, name sanitisation, per-pilot quotas, the unguessable public token, the lifecycle state machine (create / duplicate / rotate / disable / enable / snapshot / freeze / unfreeze / edit / rename / delete), the audit trail, the token-guarded artifact filesystem seam, and the embed-snippet builder. |
| `signature_stats.py` | `build_signature_payload()` — composes the render payload from **selected components only**, fully localised, reading the existing killboard authorities (`cv`, `leaderboards`, trophies). No parallel stats system. |
| `signature_render.py` | The pure Pillow compositor: `plan_layout()` (payload-free slot planner), the `_Painter`, the three layout draw functions, `render_signature_png()`, and `render_placeholder_png()`. Deterministic given `(config, payload, assets)`; no network. |
| `signature_assets.py` | The worker-side portrait / corp-logo / alliance-logo mirror fetch (the **only** network the feature performs) and the background-manifest loader. |
| `signature_pipeline.py` | The off-request engine: `signature_tick()` (beat body — stream-cursor consume, membership sweep, render the due batch), `render_one()` / `force_render()` / `rerender_all()`, `cleanup_orphans()`, and the atomic artifact write. |
| `signature_public.py` | The anonymous `GET /s/<token>.png` delivery view — three response tiers (served / pending / constant-shape 404), ETag/304, per-IP throttle. |
| `signature_views.py` | The private, owner-scoped management UI under `/killboard/signatures/`: list, builder (create/edit), synchronous preview, and the single POST action endpoint. |
| `console_signatures.py` | The admin console: render-health dashboard, settings form, background curation, and per-pilot search + moderation. |

Supporting seams: the models live in `apps/killboard/models.py`; thin Celery wrappers in
`apps/killboard/tasks.py` call the pipeline functions; the beat schedule and the two entries
are in `config/celery.py`; the env knobs are the `SIGNATURE_*` block in
`config/settings/base.py`; and the background art is produced by the
`generate_signature_backgrounds` management command (see
[the background reference](./signature-backgrounds.md)).

## Data model

- **`CombatSignature`** — one banner: owner `character`, private `name`, unique
  `public_token`, `mode` (live/snapshot), `language`, `layout`, `size_preset`, `background`
  FK, a validated `config` JSON (`components`, `period`, `featured_trophy_ids`,
  `show_timestamp`, `theme`), `status` (active/disabled/frozen), `render_status`
  (pending/ok/failed), `dirty`, `config_version`, and the failure ledger (`render_error`,
  `consecutive_failures`, `rendered_at`, `snapshot_taken_at`).
- **`SignatureBackground`** — the curated catalogue, seeded and synced from the committed
  `manifest.json` (no upload path); admins only enable/disable/reorder.
- **`CombatSignatureSettings`** — the leadership singleton (master switch, quota, refresh
  interval, snapshot toggle, revoke-on-leave, featured-trophy cap, defaults, allowed presets).
- **`SignatureScanState`** — the resume cursor (`last_seq`) into the KB-29
  `KillboardStreamEvent` ring buffer.

## Render lifecycle

```
config change ─▶ dirty=True / render_status=PENDING ─▶ signature_tick (10 min)
      ▲                                                        │
  fresh kill ── stream cursor marks live banners dirty         ▼
                                              debounced render_one
                                              (prefetch assets → payload → PNG)
                                                        │
                              locked re-check (token / config_version / status)
                                                        │
                                        atomic write (tmp + os.replace)
                                                        │
                                    nginx serves /s/<token>.png off disk
```

1. **Mark.** An owner edit (or create / rotate / enable / snapshot / admin action) sets
   `dirty=True`, `render_status=PENDING`, and — on a config-changing edit — bumps
   `config_version` and resets the failure ledger. Separately, `signature_tick` advances the
   kill-stream cursor and marks the **live** banners of pilots who scored fresh kills dirty.
2. **Pick.** `_render_due` selects active signatures that are dirty (and under the failure
   ceiling) *or* live banners whose last render predates the refresh interval, oldest first,
   capped at `SIGNATURE_RENDER_MAX_PER_TICK`.
3. **Render.** `render_one` takes a per-`(signature, config_version)` `cache.add` debounce,
   prefetches only the mirror assets the selected components need, builds the payload, and
   renders the PNG — all against a snapshot of the identity, holding no row lock.
4. **Commit.** It re-reads the row under `select_for_update`. If the token rotated, the config
   was re-edited, or the row was disabled while rendering, the bytes are **dropped**
   (`skipped_rotated`) so a rotated-away URL is never resurrected; otherwise it writes
   atomically (`tmp` + `os.replace`, 0o644 in a 0o755 dir so the read-only nginx uid can serve
   it) and resets the row to clean/OK.
5. **Fail soft.** On any error the last known-good file is kept, a **path-stripped**
   `render_error` and `consecutive_failures` advance, and after
   `SIGNATURE_RENDER_MAX_FAILURES` the picker parks the signature until its config changes or
   it is regenerated. A render fault never escapes the beat loop.
6. **Serve.** nginx serves the file directly; the Django view (`signature_public`) is only the
   fallback — a 200 pending placeholder, or a constant-shape 404 for disabled/rotated/unknown.
   Lifecycle mutations delete the file so the fallback returns the correct status.

## Components, layouts, and slots

A config lists an **ordered** subset of the closed `signatures.COMPONENTS` allowlist (at most
`MAX_COMPONENTS` = 12). Layout is a curated slot system, not a free-form canvas:

- **`plan_layout(layout, size_preset, components)`** (in `signature_render.py`) is pure and
  payload-free. It walks the components **in their declared order** and assigns each to a
  region — `header`, `rank`, `stats`, `trophies`, `meta` — up to that `(layout, size_preset)`
  pair's capacity in `_CAP`. Stat-tile components come from `_STAT_COMPONENTS`; single-slot
  components map through `_GROUP` and must be in the layout's `_SUPPORTS` set; featured
  trophies fill the trophy strip. Anything over capacity, or unsupported by the layout, is
  **dropped** (reported in `plan["dropped"]`). The builder calls `plan_layout` to warn "these
  won't fit" before any render exists; the renderer calls the same function to lay out — one
  source of truth.
- **`build_signature_payload`** emits exactly one data key plus a localised label per selected
  component, wrapped in `translation.override(language)`, reading only the authoritative
  sources (`cv.pilot_cv`, the `leaderboards` window helpers, `PilotTrophy`).
- **`render_signature_png`** builds a 2×-supersampled canvas, loads the background and (if
  present) the mirrored portrait/logos, and dispatches through `_LAYOUTS[layout]` to a
  `_layout_<name>(P, plan)` draw function that paints each region with `_Painter` helpers,
  then LANCZOS-downscales to the target preset.

## Adding a component

1. **Allowlist + catalogue.** Add the id to `signatures.COMPONENTS` (the validation
   allowlist), and to `signature_views._COMPONENT_ORDER` and `_component_labels()` (the
   builder catalogue and its localised label).
2. **Region.** Decide whether it is a **stat tile** (add it to
   `signature_stats._CARD_COMPONENTS` if it reads from the period card, to
   `signature_render._STAT_COMPONENTS`, and give it a cell in `_stat_cell`) or a
   **single-slot** component (add it to `_GROUP` and to each supporting layout's `_SUPPORTS`
   set, and adjust `_CAP` capacities if it competes for space).
3. **Payload.** Populate it in `build_signature_payload` from an **existing authority** — never
   a new parallel stats path — with an explicit fallback for a no-data pilot, and add its
   localised label. Respect the `translation.override` block.
4. **Render.** A stat cell is picked up by the grid automatically; a bespoke slot needs a
   painter routine in the relevant `_layout_*` function.
5. **Tests + i18n.** Add a structure-only render test exercising the component, a payload test
   asserting its shape and fallback, and run the extraction so the new label lands in the
   catalogues (then the de-fuzz check below).

## Adding a layout

1. Add a value + label to `CombatSignature.Layout`.
2. In `signature_render.py`: add the layout's `_SUPPORTS` set (which single-slot components it
   accepts), `_CAP` rows for **all four** presets, and a `_layout_<name>(P, plan)` draw
   function registered in `_LAYOUTS`.
3. Add structure-only render tests across all four presets, plus trace-based fit assertions if
   the layout fits or truncates text itself.

## Adding a background

Backgrounds are wholly generated, project-owned procedural art — there is no upload path. The
full procedure (a render function with a **new, unused fixed seed** and the next
`display_order`, regenerate, the `--check` determinism gate, the `text_zone_ok` contrast gate
on the safe areas, `sync_signature_backgrounds` / migrate, the tests, and keeping the
provenance table and `manifest.json` in step) is documented in
[Combat Signature Backgrounds](./signature-backgrounds.md#adding-a-design). Because the images
are generated by this repository's own code they stay under the project's MIT licence; no CCP
or third-party artwork ever enters a background (CCP-sourced pixels appear only via the
official Image Server, per the [licence review](./licence-review.md)).

## Image-testing conventions

Image tests assert **structure, never pixels** (the house rule is stated at the top of
`tests/test_signature_render.py`): exact dimensions against `PRESETS`, `img.format == "PNG"`,
and that no exception is raised — across every layout × preset, every committed background, a
missing background (flat-fill fallback), long and non-Latin names, a no-kill pilot, a missing
portrait (monogram), and empty trophies.

Content decisions are asserted through the **trace seam** rather than by inspecting pixels:
`render_signature_png(signature, payload, trace=[])` appends a labelled record for each text
decision — its `role`, the requested vs actually-drawn string, and the chosen size — so a test
can prove, for example, that a twelve-character name is not truncated by reading the trace, not
the bitmap. Background tests additionally check dimensions, sha256 checksums against the
manifest, and the safe-area contrast gate on every preset.

## The translation trap

After extracting new strings (`make messages` / `makemessages`), `msgmerge` **fuzzy-matches**
each new msgid against existing translations. A new string can silently inherit a stale, wrong
translation flagged `#, fuzzy` — and Django **ignores fuzzy entries at runtime**, so the string
renders in English until the flag is cleared, with no error anywhere.

Every extraction must therefore be followed by a **de-fuzz check**. In each catalogue only the
header entry is legitimately fuzzy, so:

```bash
grep -c '^#, fuzzy' locale/<lang>/LC_MESSAGES/django.po   # must be 1
```

A count above 1 means content msgids were fuzzy-matched — review each, correct or clear the
translation, and remove the `#, fuzzy` flag before committing. Run it for every locale.
