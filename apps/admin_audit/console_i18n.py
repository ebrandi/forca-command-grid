"""Localisation console — the Director-gated ``i18n.config`` panel.

Governs which locales the language SELECTOR offers (never English-default itself):
enable/disable each locale, pick the default + broadcast locale, and toggle browser
detection + anonymous selection. Mirrors the audit-logged POST pattern of
``console.features``; persistence goes through ``core.i18n.config.set_i18n_config``,
which re-normalises against ``settings.LANGUAGES`` and keeps English always-on.

Read-only extras surfaced beside the controls:
  * per-locale translation COVERAGE, counted from the committed ``.po`` catalogues
    (``polib`` when importable, else the dependency-light ``terminology._read_po``);
  * a CCP game-data localisation status line (honest: names are not imported yet —
    they arrive at the SDE display seam in Phase 4).
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy
from django.utils.translation import to_locale

from core import rbac
from core.audit import audit_log, client_ip
from core.i18n import config as i18n_config
from core.rbac import role_required


def _po_path(code: str) -> Path:
    """On-disk catalogue path for a language code (``pt-br`` → ``locale/pt_BR/…``)."""
    base = settings.LOCALE_PATHS[0] if settings.LOCALE_PATHS else Path(settings.BASE_DIR) / "locale"
    return Path(base) / to_locale(code) / "LC_MESSAGES" / "django.po"


def _coverage(code: str) -> dict | None:
    """Translation coverage for one locale, or ``None`` for the source language / a
    locale with no catalogue yet. Counts non-empty ``msgstr`` against the total.

    Uses ``polib`` when it imports and parses cleanly, else falls back to the built-in
    ``.po`` reader so the panel never depends on an optional package.
    """
    if code == settings.LANGUAGE_CODE:
        return None  # English is the source — every msgid IS its own translation.
    path = _po_path(code)
    if not path.exists():
        return None  # catalogue not extracted yet — report honestly, don't fake 0%.

    total = translated = 0
    parsed = False
    try:
        import polib

        entries = [e for e in polib.pofile(str(path)) if e.msgid and not e.obsolete]
        total = len(entries)
        translated = sum(1 for e in entries if (e.msgstr or "").strip())
        parsed = True
    except Exception:  # noqa: BLE001 - polib missing or a parse hiccup: use the built-in reader.
        parsed = False
    if not parsed:
        from core.i18n.terminology import _read_po

        pairs = _read_po(path)
        total = len(pairs)
        translated = sum(1 for _mid, mstr in pairs if mstr.strip())

    percent = round(translated * 100 / total) if total else 0
    return {"translated": translated, "total": total, "percent": percent}


# Honest CCP game-data status: official names are not localised in-app yet; they will be
# resolved at the SDE display seam in Phase 4 rather than shipped through a .po catalogue.
_CCP_STATUS = gettext_lazy(
    "Official CCP game-data names (ships, modules, systems) are not localised yet — they "
    "will be integrated at the SDE display seam (Phase 4)."
)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def i18n_settings(request: HttpRequest) -> HttpResponse:
    """Read/write the localisation policy that drives the language selector.

    POST persists: the enabled-locale set (checkbox grid; ``en`` is forced on), the
    default + broadcast locale, and the browser-detection / anonymous-selection toggles.
    Every locale code is validated against ``settings.LANGUAGES`` — an unknown code is
    never enabled, and an unknown default/broadcast falls back to English.
    """
    all_codes = [code for code, _ in settings.LANGUAGES]

    if request.method == "POST":
        # Checked boxes = enabled locales; anything not checked is disabled. English is
        # always on. Unknown codes (tampered form) are dropped, never persisted.
        submitted = request.POST.getlist("locale")
        invalid = [c for c in submitted if c not in all_codes]
        valid_checked = {c for c in submitted if c in all_codes}
        locales = {
            code: (code == settings.LANGUAGE_CODE or code in valid_checked)
            for code in all_codes
        }

        # default / broadcast must name a known locale that will be enabled — else English.
        default = request.POST.get("default", settings.LANGUAGE_CODE)
        if default not in all_codes or not locales.get(default):
            default = settings.LANGUAGE_CODE
        broadcast = request.POST.get("broadcast_locale", settings.LANGUAGE_CODE)
        if broadcast not in all_codes or not locales.get(broadcast):
            broadcast = settings.LANGUAGE_CODE

        browser_detection = request.POST.get("browser_detection") == "on"
        anon_selection = request.POST.get("anon_selection") == "on"

        i18n_config.set_i18n_config(
            user=request.user,
            locales=locales,
            default=default,
            broadcast_locale=broadcast,
            browser_detection=browser_detection,
            anon_selection=anon_selection,
        )
        audit_log(
            request.user, "i18n.config.update", target_type="app_setting",
            target_id=i18n_config.I18N_SETTING_KEY, ip=client_ip(request),
            metadata={
                "enabled": sorted(c for c, on in locales.items() if on),
                "default": default,
                "broadcast_locale": broadcast,
                "browser_detection": browser_detection,
                "anon_selection": anon_selection,
            },
        )
        if invalid:
            messages.error(request, gettext_lazy("Ignored unknown locale(s): %(codes)s.") % {
                "codes": ", ".join(invalid)})
        messages.success(request, gettext_lazy("Localisation settings saved."))
        return redirect("admin_audit:i18n_settings")

    cfg = i18n_config.get_i18n_config()
    native = getattr(settings, "LANGUAGE_NATIVE_NAMES", {})
    enabled = cfg["locales"]
    rows = [
        {
            "code": code,
            "label": str(label),
            "native": native.get(code, str(label)),
            "enabled": bool(enabled.get(code)),
            "is_english": code == settings.LANGUAGE_CODE,
            "coverage": _coverage(code),
        }
        for code, label in settings.LANGUAGES
    ]
    return render(request, "admin_audit/console/i18n.html", {
        "rows": rows,
        "default_locale": cfg["default"],
        "broadcast_locale": cfg["broadcast_locale"],
        "browser_detection": bool(cfg["browser_detection"]),
        "anon_selection": bool(cfg["anon_selection"]),
        "ccp_status": _CCP_STATUS,
    })
