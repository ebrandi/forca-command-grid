/* Features showcase interactivity — filter, lightbox, scroll-reveal.
   Registered as a global Alpine factory (window.showcaseGallery), matching the app's
   convention (see base.html commandPalette). Loaded first-party (CSP 'self'), no inline JS. */
(function () {
  "use strict";

  // Progressive enhancement: only arm the scroll-reveal (which starts elements at
  // opacity:0) when JS is actually running. Without this class the CSS keeps everything
  // visible, so a failed/blocked showcase.js never leaves a blank gallery. Runs
  // immediately (this file is non-deferred, before Alpine boots).
  document.documentElement.classList.add("js-sg");

  window.showcaseGallery = function () {
    return {
      filter: "all",
      lb: { open: false, src: "", alt: "", cap: "" },
      _trigger: null,

      setFilter(cat) {
        this.filter = cat;
        // Anything filtered back into view must be shown even if the reveal observer
        // already passed it while it was hidden.
        this.$root.querySelectorAll(".sg-reveal").forEach(function (el) {
          el.classList.add("is-visible");
        });
      },

      matches(cat) {
        return this.filter === "all" || this.filter === cat;
      },

      openLightbox(src, alt, cap) {
        this._trigger = document.activeElement;
        this.lb = { open: true, src: src, alt: alt || "", cap: cap || "" };
        document.body.style.overflow = "hidden";
        // Move focus into the dialog for keyboard + screen-reader users.
        this.$nextTick(function () {
          var btn = this.$refs.lbClose;
          if (btn) btn.focus();
        }.bind(this));
      },

      closeLightbox() {
        if (!this.lb.open) return;
        this.lb.open = false;
        document.body.style.overflow = "";
        // Return focus to whatever opened the lightbox.
        var t = this._trigger;
        if (t && typeof t.focus === "function") t.focus();
      },

      init() {
        var els = this.$root.querySelectorAll(".sg-reveal");
        var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (reduce || !("IntersectionObserver" in window)) {
          els.forEach(function (el) { el.classList.add("is-visible"); });
          return;
        }
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (e) {
            if (e.isIntersecting) {
              e.target.classList.add("is-visible");
              io.unobserve(e.target);
            }
          });
        }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
        els.forEach(function (el) { io.observe(el); });
      },
    };
  };
})();
