/*
 * [FORCA] Command Grid — shared Alpine component factories.
 *
 * Previously these lived in an inline <script> in base.html. Serving them from a
 * same-origin file lets the CSP drop 'unsafe-inline' for scripts; Alpine still
 * needs 'unsafe-eval' to evaluate its directives (accepted residual, R-1).
 *
 * Loaded with `defer` BEFORE alpine.min.js, so window.* factories below are
 * defined by the time Alpine initialises on DOMContentLoaded.
 */

// Item autocomplete used by the industry / logistics / stock pickers.
window.typePicker = (endpoint, initialId, initialName) => ({
  q: initialName || '', id: initialId || '', results: [], open: false,
  async lookup() {
    this.id = '';
    if (this.q.trim().length < 2) { this.results = []; this.open = false; return; }
    try {
      const sep = endpoint.includes('?') ? '&' : '?';
      const r = await fetch(endpoint + sep + 'q=' + encodeURIComponent(this.q), {
        headers: {'X-Requested-With': 'XMLHttpRequest'},
      });
      this.results = r.ok ? await r.json() : [];
    } catch (e) { this.results = []; }
    this.open = true;
  },
  choose(r) { this.q = r.name; this.id = r.type_id; this.open = false; },
});

// Multi-item autocomplete (objective metric params: industry output items). Manages a chip list of
// {id,name} and exposes a comma-joined id string via `csv` for one hidden field the server's
// list-of-ints cleaner already parses. Initial chips are read from a json_script block by id
// (CSP-safe), so an edited objective re-opens with its stored items resolved to names.
window.typeMultiPicker = (endpoint, itemsId) => ({
  q: '', results: [], open: false,
  items: (function () {
    try { return JSON.parse(document.getElementById(itemsId).textContent) || []; }
    catch (e) { return []; }
  })(),
  get csv() { return this.items.map((i) => i.id).join(','); },
  async lookup() {
    if (this.q.trim().length < 2) { this.results = []; this.open = false; return; }
    try {
      const sep = endpoint.includes('?') ? '&' : '?';
      const r = await fetch(endpoint + sep + 'q=' + encodeURIComponent(this.q),
        { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      this.results = r.ok ? await r.json() : [];
    } catch (e) { this.results = []; }
    this.open = true;
  },
  add(r) {
    if (!this.items.some((i) => String(i.id) === String(r.type_id))) {
      this.items.push({ id: String(r.type_id), name: r.name });
    }
    this.q = ''; this.results = []; this.open = false;
  },
  remove(i) { this.items.splice(i, 1); },
});

// Solar-system autocomplete (campaign staging system): one visible search box,
// one hidden id field. The server resolves the cached name from the id, so a
// selection is the only way to set it — clearing the box unsets it.
window.systemPicker = (endpoint, initId, initName) => ({
  q: initName || '', id: initId || '', results: [], open: false,
  async lookup() {
    this.id = '';
    const q = this.q.trim();
    if (q.length < 2) { this.results = []; this.open = false; return; }
    try {
      const sep = endpoint.includes('?') ? '&' : '?';
      const resp = await fetch(endpoint + sep + 'q=' + encodeURIComponent(q), { headers: { Accept: 'application/json' } });
      this.results = resp.ok ? await resp.json() : [];
    } catch (err) { this.results = []; }
    this.open = this.results.length > 0;
  },
  choose(r) { this.id = String(r.id); this.q = r.name; this.results = []; this.open = false; },
});

// Freight location picker: searches stations/structures/systems and, for a
// structure the pilot can't search, a manual (name + system) fallback. Writes
// four hidden fields: <prefix>_name / _kind / _id / _system_id.
window.locationPicker = (endpoint, prefix) => ({
  mode: 'search',
  q: '', kind: '', selId: '', selSystem: '', results: [], open: false,
  mName: '', sysQ: '', sysId: '', sysResults: [], sysOpen: false,
  clearSel() { this.kind=''; this.selId=''; this.selSystem=''; },
  async _search(q, structuresOnly) {
    if (q.trim().length < 2) return [];
    try {
      const sep = endpoint.includes('?') ? '&' : '?';
      const r = await fetch(endpoint + sep + 'q=' + encodeURIComponent(q), {headers:{'X-Requested-With':'XMLHttpRequest'}});
      let rows = r.ok ? await r.json() : [];
      if (structuresOnly) rows = rows.filter(x => x.kind === 'system');
      return rows;
    } catch (e) { return []; }
  },
  async lookup() { this.clearSel(); this.results = await this._search(this.q, false); this.open = true; },
  async sysLookup() { this.sysId=''; this.sysResults = await this._search(this.sysQ, true); this.sysOpen = true; },
  choose(r) { this.q = r.name; this.kind = r.kind; this.selId = r.id; this.selSystem = r.system_id; this.open = false; },
  chooseSys(r) { this.sysQ = r.name; this.sysId = r.system_id; this.sysOpen = false; },
  // Hidden-field values, derived from the active mode.
  outName() { return this.mode === 'manual' ? this.mName : this.q; },
  outKind() { return this.mode === 'manual' ? 'structure' : this.kind; },
  outId()   { return this.mode === 'manual' ? '' : this.selId; },
  outSystem(){ return this.mode === 'manual' ? this.sysId : this.selSystem; },
});

// Operations fleet-composition builder: browse/filter the official doctrine ships
// and add them as slots, or add a custom hull with a pasted EFT. Each slot carries
// a mandatory priority + minimum and an optional max. Catalogue and any existing
// slots are read from <script type="application/json"> blocks by id (CSP-safe).
window.fleetBuilder = (catalogueId, slotsId, shipEndpoint) => {
  const readJson = (id) => {
    try { return JSON.parse(document.getElementById(id).textContent); }
    catch (e) { return null; }
  };
  const catalogue = readJson(catalogueId) || { fits: [], categories: [], hull_classes: [], roles: [] };
  return {
    fits: catalogue.fits || [],
    categories: catalogue.categories || [],
    hullClasses: catalogue.hull_classes || [],
    roles: catalogue.roles || [],
    slots: (readJson(slotsId) || []).map((s) => ({ ...s })),
    endpoint: shipEndpoint,
    // Doctrine picker filters.
    fCategory: '', fHull: '', fRole: '', fSearch: '',
    // Custom-ship sub-form.
    customOpen: false,
    cShipQ: '', cShipId: '', cResults: [], cOpen: false, cRole: 'dps', cEft: '',
    get filteredFits() {
      const s = this.fSearch.trim().toLowerCase();
      return this.fits.filter((f) =>
        (!this.fCategory || f.category === this.fCategory) &&
        (!this.fHull || f.hull_class === this.fHull) &&
        (!this.fRole || f.role === this.fRole) &&
        (!s || (f.ship_name || '').toLowerCase().includes(s) || (f.doctrine || '').toLowerCase().includes(s))
      ).slice(0, 80);
    },
    nextPriority() {
      return this.slots.length ? Math.max(...this.slots.map((s) => (+s.priority || 1))) + 1 : 1;
    },
    hasFit(id) {
      return this.slots.some((x) => x.kind === 'doctrine' && String(x.fit_id) === String(id));
    },
    addDoctrine(f) {
      if (this.hasFit(f.fit_id)) return;
      this.slots.push({
        kind: 'doctrine', fit_id: f.fit_id, doctrine: f.doctrine, doctrine_id: f.doctrine_id,
        ship_name: f.ship_name, ship_type_id: f.ship_type_id, role: f.role || 'dps',
        eft: '', priority: this.nextPriority(), min: 1, max: '',
      });
    },
    async cLookup() {
      this.cShipId = '';
      if (this.cShipQ.trim().length < 2) { this.cResults = []; this.cOpen = false; return; }
      try {
        const sep = this.endpoint.includes('?') ? '&' : '?';
        const r = await fetch(this.endpoint + sep + 'q=' + encodeURIComponent(this.cShipQ),
          { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        this.cResults = r.ok ? await r.json() : [];
      } catch (e) { this.cResults = []; }
      this.cOpen = true;
    },
    cChoose(r) { this.cShipQ = r.name; this.cShipId = r.type_id; this.cOpen = false; },
    addCustom() {
      const name = this.cShipQ.trim();
      const eft = this.cEft.trim();
      if (!name && !eft) return;
      this.slots.push({
        kind: 'custom', fit_id: '', doctrine: '', doctrine_id: '',
        ship_name: name || 'Custom ship', ship_type_id: this.cShipId || '',
        role: this.cRole, eft: eft, priority: this.nextPriority(), min: 1, max: '',
      });
      this.cShipQ = ''; this.cShipId = ''; this.cEft = ''; this.cResults = [];
      this.cOpen = false; this.customOpen = false;
    },
    remove(i) { this.slots.splice(i, 1); },
    get slotMinTotal() { return this.slots.reduce((a, s) => a + (+s.min || 0), 0); },
  };
};

// Planetary Industry plan wizard — dynamic planet rows (planet type + role +
// product picker), all client-side against the small (~83) material catalogue.
window.planetBuilder = (planetsId, materialsId, existingId, defaultRole) => {
  const readJson = (id) => {
    try { return JSON.parse(document.getElementById(id).textContent); }
    catch (e) { return null; }
  };
  const planetTypes = readJson(planetsId) || [];
  const materials = readJson(materialsId) || [];
  const existing = readJson(existingId) || [];
  const blank = () => ({ slug: '', role: defaultRole || 'extract', product_id: '', product_name: '', q: '', open: false });
  return {
    planetTypes,
    materials,
    rows: existing.length
      ? existing.map((r) => ({ slug: r.slug, role: r.role, product_id: r.product_id, product_name: r.product_name, q: r.product_name || '', open: false }))
      : [blank()],
    tierColor(t) {
      return ({ P0: 'text-faint', P1: 'text-cyan', P2: 'text-gold', P3: 'text-win', P4: 'text-goldb' })[t] || 'text-muted';
    },
    planetFor(slug) { return this.planetTypes.find((p) => p.slug === slug); },
    resultsFor(row) {
      const s = (row.q || '').trim().toLowerCase();
      const list = s ? this.materials.filter((m) => m.name.toLowerCase().includes(s)) : this.materials;
      return list.slice(0, 14);
    },
    choose(row, m) { row.product_id = m.type_id; row.product_name = m.name; row.q = m.name; row.open = false; },
    clearProduct(row) { row.product_id = ''; row.product_name = ''; row.q = ''; },
    add() { this.rows.push(blank()); },
    remove(i) { this.rows.splice(i, 1); if (!this.rows.length) this.add(); },
  };
};

/*
 * Progressive enhancement for the tightened CSP. Inline on* handlers (onchange,
 * onsubmit) are blocked once 'unsafe-inline' is dropped from script-src, so the
 * two behaviours that used them are wired here from data-* attributes instead:
 *
 *   data-autosubmit          submit the control's own form when it changes
 *                            (filter / sort / character dropdowns)
 *   <form data-confirm="…">  ask for confirmation before a destructive submit
 *
 * Loaded with `defer`, so the DOM is already parsed when this runs.
 */
(function () {
  function wire() {
    document.querySelectorAll('[data-autosubmit]').forEach(function (el) {
      el.addEventListener('change', function () { if (el.form) el.form.submit(); });
    });
    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (e) {
        if (!window.confirm(form.getAttribute('data-confirm'))) e.preventDefault();
      });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();

/*
 * Sidebar accordion (CSP-safe, no inline script). Each [data-acc] group has a
 * [data-acc-toggle] header and a [data-acc-body]. Open/closed state lives in
 * data-open (CSS hides the body when "0") and persists per group key in
 * localStorage. The group containing the active page is always forced open, so
 * navigating within a section keeps its siblings visible.
 */
(function () {
  var KEY = 'forca:nav';
  function load() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
  }
  function save(state) {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* private mode */ }
  }
  function setOpen(group, open) {
    group.setAttribute('data-open', open ? '1' : '0');
    var btn = group.querySelector('[data-acc-toggle]');
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  function holdsActivePage(group) {
    var path = window.location.pathname;
    var links = group.querySelectorAll('a[href]');
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute('href');
      if (href && href !== '/' && (path === href || path.indexOf(href) === 0)) return true;
    }
    return false;
  }
  function initAccordion() {
    var groups = document.querySelectorAll('[data-acc]');
    if (!groups.length) return;
    var state = load();
    groups.forEach(function (group) {
      var key = group.getAttribute('data-acc-key');
      var active = holdsActivePage(group);
      // Active group always open; otherwise honour the stored choice, defaulting to
      // CLOSED so the full set of group headers fits on screen (the sidebar has ~11
      // groups; defaulting them open overflowed the rail). Users expand what they need.
      var open = active ? true : (key in state ? !!state[key] : false);
      setOpen(group, open);
      var btn = group.querySelector('[data-acc-toggle]');
      if (btn) {
        btn.addEventListener('click', function () {
          var nowOpen = group.getAttribute('data-open') !== '1';
          setOpen(group, nowOpen);
          var s = load();
          s[key] = nowOpen;
          save(s);
        });
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAccordion);
  } else {
    initAccordion();
  }
})();

/*
 * Shared Chart.js theming + helpers (D11). chart.umd.js is loaded PER-PAGE, not
 * globally, so nothing here may reference the global `Chart` at load time — every
 * function is called from a page's nonce'd init script AFTER chart.umd.js has run.
 * Consolidates the colour map, 12-colour palette, json_script reader, ISK
 * formatter, Chart.defaults setup, and the doughnut factory that were copy-pasted
 * across the eight chart pages. Pages keep their chart-type-specific `new Chart()`
 * calls and any divergent palette (passed via opts.palette).
 */
window.forcaChart = {
  mono: "'JetBrains Mono', ui-monospace, monospace",
  colors: {
    kill: '#3fb950', win: '#3fb950', loss: '#f0533f', gold: '#f4a52b',
    goldFaint: 'rgba(244,165,43,.55)', cyan: '#46cfe0', line: '#222d3e',
    faint: '#5a6678', ink: '#e8eef6', panel: '#10151f',
  },
  // Canonical 12-colour categorical palette (doughnut slices, multi-series).
  palette: ['#46cfe0', '#f4a52b', '#3fb950', '#f0533f', '#9d7bd8', '#e0a946',
            '#5aa9e6', '#7ddc8b', '#e87fa5', '#8a98ab', '#c0c8d4', '#6fb3d2'],
  // Read a {{ data|json_script:"id" }} block; null when absent/empty.
  read: function (id) {
    var el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  },
  // Compact ISK: 1.2T / 3.4B / 56M / 7K.
  isk: function (v) {
    var a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(1) + 'T';
    if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return (v / 1e6).toFixed(0) + 'M';
    if (a >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return '' + v;
  },
  // Set the shared font/colour defaults; returns false (so callers can early-out)
  // when chart.umd.js hasn't loaded. Pass {legendBox:true} for the compact legend.
  applyDefaults: function (opts) {
    if (typeof Chart === 'undefined') return false;
    Chart.defaults.font.family = this.mono;
    Chart.defaults.color = this.colors.faint;
    if (opts && opts.legendBox) {
      Chart.defaults.plugins.legend.labels.boxWidth = 10;
      Chart.defaults.plugins.legend.labels.boxHeight = 10;
    }
    // D7: clear a canvas's .skeleton placeholder once ANY chart first renders —
    // works for the inline line/bar/mixed charts without per-chart wiring.
    if (!this._skeletonPlugin) {
      this._skeletonPlugin = true;
      var self = this;
      Chart.register({
        id: 'forcaSkeleton',
        afterRender: function (chart) {
          if (chart.canvas && chart.canvas.id) self.clearSkeleton(chart.canvas.id);
        },
      });
      // D7 safety net: inline charts render synchronously on this page, so by the
      // load event any .skeleton STILL wrapping a <canvas> is a chart that was
      // guarded out for empty data and will never render. Strip its placeholder so
      // the panel shows its empty state instead of pulsing forever. Scoped to
      // canvas wrappers, so non-chart skeletons (if any) are left untouched.
      window.addEventListener('load', function () {
        var nodes = document.querySelectorAll('.skeleton');
        for (var i = 0; i < nodes.length; i++) {
          if (nodes[i].querySelector('canvas')) nodes[i].classList.remove('skeleton');
        }
      });
    }
    return true;
  },
  // Doughnut over [{name, count}] rows. opts: {cutout, legend:'bottom', palette}.
  doughnut: function (canvasId, rows, opts) {
    var el = document.getElementById(canvasId);
    this.clearSkeleton(canvasId);  // clear whether or not there's data to draw
    if (!el || !rows || !rows.length) return null;
    opts = opts || {};
    var pal = opts.palette || this.palette;
    var chart = new Chart(el, {
      type: 'doughnut',
      data: {
        labels: rows.map(function (r) { return r.name; }),
        datasets: [{
          data: rows.map(function (r) { return r.count; }),
          backgroundColor: rows.map(function (_, i) { return pal[i % pal.length]; }),
          borderColor: this.colors.panel, borderWidth: 2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: opts.cutout || '58%',
        plugins: {
          legend: opts.legend === 'bottom'
            ? { position: 'bottom', labels: { color: this.colors.faint, padding: 8 } }
            : { display: false },
          tooltip: { callbacks: { label: function (ctx) { return ctx.label + ': ' + ctx.parsed; } } },
        },
      },
    });
    this.clearSkeleton(canvasId);
    return chart;
  },
  // D7: strip the loading placeholder from a canvas's wrapper once it has rendered.
  clearSkeleton: function (canvasId) {
    var el = document.getElementById(canvasId);
    var wrap = el && el.closest('.skeleton');
    if (wrap) wrap.classList.remove('skeleton');
  },
};

/*
 * Command palette (D8) — Cmd/Ctrl-K quick-jump. Rather than duplicate the nav in
 * JS, it scrapes the already-rendered, role/feature-gated nav links (and the Admin
 * Console hub cards when on that page) at open time, so the index inherits every
 * {% if features.* %}/is_officer/is_director gate for free. The nav is included
 * twice (desktop + mobile drawer), so dedupe by href. CSP-clean: this factory is
 * first-party, the overlay uses only Alpine directives (no inline <script>).
 */
window.commandPalette = () => ({
  open: false,
  q: '',
  sel: 0,
  items: [],
  harvest() {
    var seen = new Map();
    document.querySelectorAll('.navlink').forEach(function (a) {
      var url = a.getAttribute('href');
      if (!url || seen.has(url)) return;
      var g = a.closest('[data-acc]');
      var eb = g ? g.querySelector('.navgroup .eyebrow') : null;
      var use = a.querySelector('.navicon use');
      seen.set(url, {
        label: a.textContent.trim(),
        url: url,
        group: eb ? eb.textContent.trim() : (g ? (g.getAttribute('data-acc-key') || '') : ''),
        icon: use ? use.getAttribute('href') : '',
      });
    });
    document.querySelectorAll('a.panel').forEach(function (a) {
      var url = a.getAttribute('href');
      var h = a.querySelector('h2, h3');
      if (!url || !h || seen.has(url)) return;
      var sec = a.closest('section');
      var eb = sec ? sec.querySelector('.eyebrow') : null;
      var use = a.querySelector('use');
      seen.set(url, {
        label: h.textContent.trim(),
        url: url,
        group: eb ? eb.textContent.trim() : 'Admin',
        icon: use ? use.getAttribute('href') : '',
      });
    });
    this.items = Array.from(seen.values()).filter(function (it) { return it.label; });
  },
  get filtered() {
    var s = this.q.trim().toLowerCase();
    var list = !s ? this.items : this.items.filter(function (it) {
      return it.label.toLowerCase().indexOf(s) >= 0 || (it.group || '').toLowerCase().indexOf(s) >= 0;
    });
    return list.slice(0, 40);
  },
  show() {
    this.harvest();
    this.q = '';
    this.sel = 0;
    this.open = true;
    var self = this;
    this.$nextTick(function () { if (self.$refs.q) self.$refs.q.focus(); });
  },
  hide() { this.open = false; },
  move(d) {
    var n = this.filtered.length;
    if (n) this.sel = (this.sel + d + n) % n;
  },
  go() {
    var it = this.filtered[this.sel];
    if (it) window.location.href = it.url;
  },
});

/*
 * Client-side sortable tables (D6). Server-rendered rows already exist in the
 * DOM, so a click just reorders <tr data-row> nodes — no fetch/htmx. Each sortable
 * cell carries data-sort-key + data-sort-value (the RAW numeric/string value, NOT
 * the |isk/|duration display text); headers call sortBy(key). Numeric when both
 * compared values parse as numbers, else a locale string compare. CSP-clean: a
 * global factory referenced from x-data (Alpine's retained unsafe-eval evaluates
 * the @click expressions). x-ref="body" on the <tbody> (falls back to the first
 * <tbody>). initialKey/initialDir only pre-set the indicator to match the
 * server's default order; they don't re-sort.
 */
window.sortableTable = (initialKey, initialDir) => ({
  key: initialKey || '',
  dir: initialDir || 'asc',
  sortBy(col) {
    if (this.key === col) {
      this.dir = this.dir === 'asc' ? 'desc' : 'asc';
    } else {
      this.key = col;
      this.dir = 'asc';
    }
    var body = this.$refs.body || this.$el.querySelector('tbody');
    if (!body) return;
    var key = this.key;
    var mul = this.dir === 'asc' ? 1 : -1;
    var rows = Array.prototype.slice.call(body.querySelectorAll('tr[data-row]'));
    rows.sort(function (a, b) {
      var ac = a.querySelector('[data-sort-key="' + key + '"]');
      var bc = b.querySelector('[data-sort-key="' + key + '"]');
      var av = ac ? (ac.getAttribute('data-sort-value') || '') : '';
      var bv = bc ? (bc.getAttribute('data-sort-value') || '') : '';
      var an = parseFloat(av), bn = parseFloat(bv);
      var num = !isNaN(an) && !isNaN(bn);
      return (num ? an - bn : String(av).localeCompare(String(bv))) * mul;
    });
    rows.forEach(function (r) { body.appendChild(r); });
  },
  // For :aria-sort on a header cell.
  ariaSort(col) {
    return this.key === col ? (this.dir === 'asc' ? 'ascending' : 'descending') : 'none';
  },
});

// D9: interactive line sparkline. Takes RAW values; normalises to the 120x28
// viewBox internally. Hover tracks the nearest point and shows value (+ unit).
window.sparkline = (values, opts) => ({
  values: (Array.isArray(values) ? values : []).map(Number).filter(function (n) { return isFinite(n); }),
  unit: (opts && opts.unit) || '',
  labels: (opts && opts.labels) || [],
  w: 120,
  h: 28,
  pad: 3,
  hover: -1,
  get pts() {
    var v = this.values;
    if (v.length < 2) return [];
    var max = Math.max.apply(null, v);
    var min = Math.min.apply(null, v);
    var span = max - min;
    var n = v.length;
    var innerW = this.w - this.pad * 2;
    var innerH = this.h - this.pad * 2;
    var self = this;
    return v.map(function (val, i) {
      var x = self.pad + (i / (n - 1)) * innerW;
      var y = span === 0 ? self.pad + innerH / 2 : self.pad + innerH - ((val - min) / span) * innerH;
      return { x: x, y: y, val: val };
    });
  },
  get linePoints() {
    return this.pts.map(function (p) { return p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join(' ');
  },
  get areaPath() {
    var p = this.pts;
    if (!p.length) return '';
    var base = (this.h - this.pad).toFixed(1);
    var d = 'M' + p[0].x.toFixed(1) + ',' + base;
    for (var i = 0; i < p.length; i++) d += ' L' + p[i].x.toFixed(1) + ',' + p[i].y.toFixed(1);
    d += ' L' + p[p.length - 1].x.toFixed(1) + ',' + base + ' Z';
    return d;
  },
  get hoverPt() {
    return this.hover >= 0 && this.hover < this.pts.length ? this.pts[this.hover] : null;
  },
  get hoverLabel() {
    if (this.hover < 0) return '';
    var lab = this.labels[this.hover];
    var val = this.values[this.hover];
    return (lab ? lab + ': ' : '') + val + (this.unit ? ' ' + this.unit : '');
  },
  track(ev) {
    var n = this.values.length;
    if (n < 2) { this.hover = -1; return; }
    var rect = ev.currentTarget.getBoundingClientRect();
    if (!rect.width) { this.hover = -1; return; }
    var rel = (ev.clientX - rect.left) / rect.width;
    this.hover = Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1))));
  },
  leave() { this.hover = -1; },
});
