/**
 * Tailwind build for [FORCA] Command Grid.
 *
 * This replaces the former Tailwind Play CDN (cdn.tailwindcss.com), which
 * required 'unsafe-inline'/'unsafe-eval' and pulled an executable script from a
 * third party on every page load. The theme below is the exact config that used
 * to live inline in templates/base.html.
 *
 * Content globs must cover every place a class name can appear so the JIT keeps
 * it: server templates and the hand-written JS (app.js + per-page inline
 * scripts build class strings for Alpine :class bindings).
 */
module.exports = {
  content: [
    "../templates/**/*.html",
    "../apps/**/templates/**/*.html",
    "../static/js/**/*.js",
  ],
  // Security-band text colours are emitted from a Python template filter
  // (apps/sde/templatetags/eve.py::sec_class), which the content scanner above
  // never sees, so the JIT would purge them. Safelist keeps them compiled.
  safelist: ["text-sechi", "text-secmid", "text-seclo", "text-secnull"],
  theme: {
    extend: {
      colors: {
        space: "#0a0e16", panel: "#10151f", panel2: "#161d29", line: "#222d3e",
        gold: "#f4a52b", goldb: "#ffc861", cyan: "#46cfe0",
        kill: "#3fb950", loss: "#f0533f", win: "#34d399",
        ink: "#e8eef6", muted: "#8a98ab", faint: "#5a6678",
        // EVE security bands (system security status, high→null):
        sechi: "#46cfe0",   // 1.0–0.8  cyan
        secmid: "#e8d64a",  // 0.7–0.5  yellow
        seclo: "#f5872e",   // 0.4–0.1  orange
        secnull: "#f0533f", // 0.0–-1.0 red
      },
      fontFamily: {
        display: ['"Chakra Petch"', "ui-sans-serif", "system-ui"],
        sans: ["Inter", "ui-sans-serif", "system-ui"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
