# Frontend build

This directory builds the **self-hosted** front-end assets so the app serves no
third-party scripts (CSP hardening).
Production has **no Node step**: the build outputs are committed under
`../static/` and shipped as-is (collectstatic serves them).

## What it produces

- `../static/css/app.css` — Tailwind compiled from `app.css` + `tailwind.config.js`
  (the theme that used to live inline in `templates/base.html`).
- `../static/js/vendor/{alpine.min,htmx.min,chart.umd,svg-pan-zoom.min}.js` —
  the pinned runtime libraries, copied from `node_modules` (sourcemap comments
  stripped so Django's ManifestStaticFilesStorage doesn't look for absent `.map`s).

`../static/js/app.js` (shared Alpine factories + the `data-autosubmit` /
`data-confirm` progressive-enhancement wiring) is **hand-written**, not generated.

## Rebuild (after changing templates' classes or bumping a library)

```bash
cd frontend
npm install            # first time only
npm run build          # vendor libs + compile + minify CSS
```

Then commit the changed files under `../static/` and deploy. Bump library versions
in `package.json` (kept in lockstep with the CSP allowlist, which is just `'self'`).

> If you add Tailwind classes that only appear in newly-built dynamic strings,
> re-run `npm run build` so the JIT keeps them; the content globs in
> `tailwind.config.js` already cover `templates/`, `apps/**/templates/`, and
> `static/js/`.
