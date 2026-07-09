/*
 * Copy the pinned runtime JS libraries out of node_modules into
 * ../static/js/vendor/ so the app serves them same-origin instead of pulling
 * executable scripts from unpkg.com (CSP hardening, R-1).
 *
 * Run via `npm run vendor`. The copied files are committed to the repo; prod
 * has no Node build step (collectstatic ships them as-is).
 */
const fs = require("fs");
const path = require("path");

const OUT = path.join(__dirname, "..", "static", "js", "vendor");
fs.mkdirSync(OUT, { recursive: true });

// [source-in-node_modules, destination-filename]
const FILES = [
  ["alpinejs/dist/cdn.min.js", "alpine.min.js"],
  ["htmx.org/dist/htmx.min.js", "htmx.min.js"],
  ["chart.js/dist/chart.umd.js", "chart.umd.js"],
  ["svg-pan-zoom/dist/svg-pan-zoom.min.js", "svg-pan-zoom.min.js"],
];

// Strip trailing `//# sourceMappingURL=…` / `//@ sourceMappingURL=…` comments:
// we don't ship the .map files, and Django's ManifestStaticFilesStorage fails
// collectstatic when an asset references a sibling file that isn't present.
const SOURCEMAP_RE = /^\s*\/\/[#@]\s*sourceMappingURL=.*$/gm;

for (const [src, dest] of FILES) {
  const from = path.join(__dirname, "node_modules", src);
  const to = path.join(OUT, dest);
  const code = fs.readFileSync(from, "utf8").replace(SOURCEMAP_RE, "").trimEnd() + "\n";
  fs.writeFileSync(to, code);
  const kb = (fs.statSync(to).size / 1024).toFixed(1);
  console.log(`vendored ${dest}  (${kb} KiB)`);
}
