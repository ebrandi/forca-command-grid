"""Leader-configurable localisation policy — the ``i18n.config`` app setting.

Mirrors the ``core.features`` pattern: a single ``AppSetting`` row read through a
per-process Redis cache (TTL 600s, busted on save), so the per-request locale
resolution costs a cache read, not a query.

The config is the second of three rollout gates (docs/i18n/03-decisions.md D3):

    I18N_ENABLED (env kill switch)  →  LANGUAGES (framework set)  →  i18n.config
                                                                     (this: which
                                                                      locales the
                                                                      selector offers)

English (``en``) is always available and can never be disabled — it is the
canonical source language and the terminal fallback. Nothing here ever activates
a locale that is not both in ``settings.LANGUAGES`` and enabled below.
"""
from __future__ import annotations

from django.conf import settings

I18N_SETTING_KEY = "i18n.config"
_CACHE_KEY = "i18n:config:v1"
_CACHE_TTL = 600

# Shape + safe defaults. Ship with English only enabled so nothing user-visible
# changes until leadership turns a validated locale on (progressive reveal).
_DEFAULTS: dict = {
    "enabled": True,
    "locales": {"en": True},
    "default": "en",
    "broadcast_locale": "en",
    "browser_detection": True,
    "anon_selection": True,
}
_SCALAR_KEYS = ("enabled", "default", "broadcast_locale", "browser_detection", "anon_selection")


def _all_codes() -> list[str]:
    return [code for code, _ in settings.LANGUAGES]


def get_i18n_config() -> dict:
    """The effective config (cached), normalised against ``settings.LANGUAGES``."""
    from django.core.cache import cache

    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return dict(cached)

    from apps.admin_audit.models import AppSetting

    stored = AppSetting.get(I18N_SETTING_KEY, {}) or {}
    cfg = dict(_DEFAULTS)
    for key in _SCALAR_KEYS:
        if key in stored:
            cfg[key] = stored[key]

    stored_locales = stored.get("locales") if isinstance(stored.get("locales"), dict) else {}
    locales: dict[str, bool] = {}
    for code in _all_codes():
        locales[code] = bool(stored_locales.get(code, _DEFAULTS["locales"].get(code, False)))
    locales["en"] = True  # never disable the canonical fallback
    cfg["locales"] = locales

    # default / broadcast must name an enabled, known locale — else English.
    if cfg["default"] not in locales or not locales[cfg["default"]]:
        cfg["default"] = "en"
    if cfg["broadcast_locale"] not in locales or not locales[cfg["broadcast_locale"]]:
        cfg["broadcast_locale"] = "en"

    cache.set(_CACHE_KEY, cfg, _CACHE_TTL)
    return dict(cfg)


def is_i18n_enabled() -> bool:
    """False when the env kill switch or the config master flag is off."""
    if not getattr(settings, "I18N_ENABLED", True):
        return False
    return bool(get_i18n_config().get("enabled", True))


def enabled_locales() -> list[str]:
    """Codes the selector offers: enabled in config ∩ in ``LANGUAGES`` (``en`` always)."""
    if not is_i18n_enabled():
        return ["en"]
    cfg = get_i18n_config()
    out = [code for code in _all_codes() if cfg["locales"].get(code)]
    if "en" not in out:
        out.insert(0, "en")
    return out


def available_locales() -> list[dict]:
    """Selector rows ``[{code, label, native}]`` for enabled locales, English first."""
    enabled = set(enabled_locales())
    native = getattr(settings, "LANGUAGE_NATIVE_NAMES", {})
    rows = []
    for code, label in settings.LANGUAGES:
        if code in enabled:
            rows.append({"code": code, "label": str(label), "native": native.get(code, str(label))})
    return rows


def default_locale() -> str:
    return get_i18n_config().get("default", "en")


def broadcast_locale() -> str:
    """The single locale used for group/broadcast messages with no one recipient (D14)."""
    return get_i18n_config().get("broadcast_locale", "en")


def anon_can_select() -> bool:
    """Whether anonymous visitors may pick a language from the selector."""
    return bool(get_i18n_config().get("anon_selection", True))


def set_i18n_config(*, user=None, **changes) -> dict:
    """Persist config changes (merge + normalise) and bust the cache."""
    from django.core.cache import cache

    from apps.admin_audit.models import AppSetting

    cfg = get_i18n_config()
    for key in _SCALAR_KEYS:
        if key in changes:
            cfg[key] = changes[key]
    if isinstance(changes.get("locales"), dict):
        for code, on in changes["locales"].items():
            if code in cfg["locales"]:
                cfg["locales"][code] = bool(on)
        cfg["locales"]["en"] = True
    AppSetting.objects.update_or_create(
        key=I18N_SETTING_KEY, defaults={"value": cfg, "updated_by": user}
    )
    cache.delete(_CACHE_KEY)
    return cfg
