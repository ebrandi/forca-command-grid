"""Combat Signatures — the private, owner-scoped management UI (WS-6, plan A15/A16/A17).

Everything a pilot needs to build, preview, embed and manage their banner images lives here under
``/killboard/signatures/`` (module-per-concern, like :mod:`signature_public`). The views are THIN:
every mutation routes through :mod:`signatures` domain functions (which own validation, the LP-4
ownership ceiling and the audit trail), the builder warnings come from the payload-free
:func:`signature_render.plan_layout`, and the synchronous preview reuses
:func:`signature_stats.build_signature_payload` + :func:`signature_render.render_signature_png` on
an UNSAVED instance so nothing is written.

Gating (A17), applied by :func:`_gate` on every view: ``@login_required`` (anonymous → login
redirect), the ``killboard`` feature flag AND ``CombatSignatureSettings.enabled`` (either off →
404, the killboard 404 style), and an acting home-corp pilot (a non-member is already confined to
the recruitment surface by ``MembershipGateMiddleware``). Ownership is the query: every signature
is fetched ``filter(character_id=acting_pilot)`` so another account's — or a linked-but-not-acting
pilot's — signature simply 404s (IDOR-safe, threat model).
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import pilots
from core.audit import audit_log, client_ip

from . import signatures, tasks
from .models import (
    CombatSignature,
    CombatSignatureSettings,
    PilotTrophy,
    SignatureBackground,
)
from .signature_render import plan_layout, render_signature_png
from .signature_stats import build_signature_payload

# Canonical builder ordering for the component catalogue (A16). Plain ids — labels are built
# per-request under the active locale by :func:`_component_labels` (module-level ``_()`` would
# freeze the label at import-time locale).
_COMPONENT_ORDER = (
    "portrait", "pilot_name", "corp", "alliance",
    "kills", "losses", "solo_kills", "final_blows",
    "isk_destroyed", "isk_lost", "isk_efficiency", "kd_ratio",
    "rank_title", "rank_progress", "trophies_featured", "trophy_count",
    "last_kill", "best_kill", "favourite_ship", "top_ship_class",
    "activity_period_label", "stats_timestamp",
)

# A sensible starter selection for a brand-new signature (all fit the default identity/standard).
_DEFAULT_COMPONENTS = ("portrait", "pilot_name", "corp", "kills", "losses", "isk_destroyed")

# Per-asset fetch budget for the interactive preview (seconds). Short so a cold portrait/logo fetch
# can't tie up a web worker; the Celery render keeps the full default. Assets cache for a week, so
# only a pilot's first-ever preview can pay it, and it degrades to the monogram on timeout.
_PREVIEW_FETCH_TIMEOUT = 4

# The POST-action verbs the single action endpoint accepts (each owner-gated in the domain layer).
_ACTIONS = frozenset({
    "regenerate", "duplicate", "snapshot", "rotate", "disable", "enable", "delete",
})


# --------------------------------------------------------------------------- #
#  Catalogue / labels (per-request locale)
# --------------------------------------------------------------------------- #
def _component_labels() -> dict[str, str]:
    """The human labels for every A16 component, under the active locale."""
    return {
        "portrait": _("Portrait"),
        "pilot_name": _("Pilot name"),
        "corp": _("Corporation"),
        "alliance": _("Alliance"),
        "kills": _("Kills"),
        "losses": _("Losses"),
        "solo_kills": _("Solo kills"),
        "final_blows": _("Final blows"),
        "isk_destroyed": _("ISK destroyed"),
        "isk_lost": _("ISK lost"),
        "isk_efficiency": _("ISK efficiency"),
        "kd_ratio": _("Kill/Death ratio"),
        "rank_title": _("Rank title"),
        "rank_progress": _("Rank progress"),
        "trophies_featured": _("Featured trophies"),
        "trophy_count": _("Trophy count"),
        "last_kill": _("Last kill"),
        "best_kill": _("Best kill"),
        "favourite_ship": _("Favourite ship"),
        "top_ship_class": _("Top ship class"),
        "activity_period_label": _("Activity period label"),
        "stats_timestamp": _("Stats timestamp"),
    }


def _period_choices() -> list[tuple[str, str]]:
    """Activity-window options for the period selector (labels localised per request)."""
    return [
        ("7d", _("Last 7 days")),
        ("30d", _("Last 30 days")),
        ("90d", _("Last 90 days")),
        ("month", _("This month")),
        ("lastmonth", _("Last month")),
        ("all", _("All time")),
    ]


def _theme_choices() -> list[tuple[str, str]]:
    return [("gold", _("Gold")), ("cyan", _("Cyan")), ("kill", _("Green"))]


# --------------------------------------------------------------------------- #
#  Gating & ownership
# --------------------------------------------------------------------------- #
def _gate(request: HttpRequest) -> tuple:
    """The shared feature/membership gate. Returns ``(acting_pilot, settings)`` or raises Http404.

    ``@login_required`` handles the anonymous case (login redirect) upstream; a logged-in non-member
    never reaches here (``MembershipGateMiddleware`` sends them to onboarding). The remaining checks
    404 in the killboard style: the killboard feature flag, the DB master switch, and — defence in
    depth against a member flying a non-member alt — a current home-corp acting pilot.
    """
    from core.features import feature_enabled

    if not feature_enabled("killboard"):
        raise Http404(_("This feature is not enabled for this corporation."))
    cfg = CombatSignatureSettings.load()
    if not cfg.enabled:
        raise Http404(_("Combat Signatures are not enabled."))
    pilot = pilots.acting_pilot(request.user)
    if pilot is None or not pilot.is_corp_member:
        raise Http404(_("Combat Signatures are not available."))
    return pilot, cfg


def _owned_signature(pilot, pk) -> CombatSignature:
    """The acting pilot's signature ``pk``, or Http404. The ownership check IS the query — another
    account's (or a linked-but-not-acting pilot's) row never resolves (IDOR-safe)."""
    sig = (
        CombatSignature.objects.select_related("background")
        .filter(pk=pk, character_id=pilot.character_id)
        .first()
    )
    if sig is None:
        raise Http404(_("No such signature."))
    return sig


# --------------------------------------------------------------------------- #
#  List
# --------------------------------------------------------------------------- #
def _public_url(request: HttpRequest, sig: CombatSignature) -> str:
    """The absolute public banner URL for ``sig`` (request context — WS-5 contract)."""
    return request.build_absolute_uri(reverse("signature_public", args=[sig.public_token]))


def _row(request: HttpRequest, sig: CombatSignature) -> dict:
    """The per-signature view-model for the list page (thumbnail, chips, embed snippets)."""
    url = _public_url(request, sig)
    alt = signatures.signature_alt_text(sig)
    # The public URL serves a valid PNG for ACTIVE/FROZEN rows (the pending placeholder until the
    # first render lands); a DISABLED row's URL 404s, so show a static placeholder there instead of
    # a broken image.
    servable = sig.status in (CombatSignature.Status.ACTIVE, CombatSignature.Status.FROZEN)
    return {
        "sig": sig,
        "public_url": url,
        "alt": alt,
        "servable": servable,
        "config_locked": sig.mode == CombatSignature.Mode.SNAPSHOT,
        "embed": signatures.embed_snippets(url, alt),
    }


@login_required
def signature_list(request: HttpRequest) -> HttpResponse:
    """A pilot's Combat Signatures: every status, with per-row actions and embed snippets."""
    pilot, cfg = _gate(request)
    sigs = list(
        CombatSignature.objects.select_related("background")
        .filter(character_id=pilot.character_id)
        .order_by("-updated_at")
    )
    rows = [_row(request, s) for s in sigs]
    active_count = sum(1 for s in sigs if s.status == CombatSignature.Status.ACTIVE)
    return render(request, "killboard/signatures/list.html", {
        "rows": rows,
        "active_count": active_count,
        "max_active": cfg.max_active_per_pilot,
        "at_quota": active_count >= cfg.max_active_per_pilot,
        "snapshots_enabled": cfg.snapshots_enabled,
    })


# --------------------------------------------------------------------------- #
#  Builder (create + edit share one template)
# --------------------------------------------------------------------------- #
def _size_preset_choices(cfg) -> list[tuple[str, str]]:
    """The allowed size presets as ``(value, label)`` in the model's declared order."""
    allowed = set(cfg.allowed_size_presets or [])
    return [
        (value, label) for value, label in CombatSignature.SizePreset.choices
        if not allowed or value in allowed
    ]


def _background_options() -> list[SignatureBackground]:
    return list(SignatureBackground.objects.filter(enabled=True).order_by("display_order", "key"))


def _trophy_options(character_id) -> list[dict]:
    """The pilot's EARNED trophies for the featured picker: ``definition_id`` + name + tier.

    ``trophies.pilot_trophies`` omits the numeric definition id the config stores, so read
    ``PilotTrophy`` directly (read-only, owner-scoped).
    """
    rows = (
        PilotTrophy.objects.filter(character_id=character_id)
        .select_related("definition").order_by("-awarded_at")
    )
    return [
        {"id": pt.definition_id, "name": pt.definition.name, "tier": pt.definition.tier}
        for pt in rows
    ]


def _component_items(selected: list[str]) -> list[dict]:
    """The ordered component catalogue for the builder: selected ids first (in their saved order),
    then the rest of the allowlist. Each row is ``{id, label, checked}`` — the Alpine widget renders
    them in array order and browsers submit the checked boxes in DOM order, so a plain
    ``getlist('components')`` recovers the pilot's ordering without any drag-drop."""
    labels = _component_labels()
    selected = [c for c in selected if c in signatures.COMPONENTS]
    seen = set(selected)
    ordered = selected + [c for c in _COMPONENT_ORDER if c not in seen]
    return [{"id": c, "label": str(labels[c]), "checked": c in seen} for c in ordered]


def _warn_dropped(request: HttpRequest, config: dict, layout: str, size_preset: str) -> None:
    """Attach a non-blocking warning for every component that overflows this layout+preset."""
    for text in _overflow_warnings(config, layout, size_preset):
        messages.warning(request, text)


def _overflow_warnings(config: dict, layout: str, size_preset: str) -> list[str]:
    """The human overflow warnings for a config under ``plan_layout`` (empty when everything fits)."""
    plan = plan_layout(layout, size_preset, config.get("components", []))
    if not plan["dropped"]:
        return []
    labels = _component_labels()
    return [
        _("“%(component)s” will not fit the selected layout and size and won't be shown.")
        % {"component": labels.get(comp, comp)}
        for comp in plan["dropped"]
    ]


def _parse_builder(request: HttpRequest, cfg, pilot) -> dict:
    """Read + validate the builder POST into domain-ready kwargs. Raises ``ValidationError``.

    Every field is validated: the name via :func:`signatures.sanitize_name`, the config via
    :func:`signatures.validate_config` (allowlist, dedupe, caps, cross-field layout/preset checks),
    the background against the enabled set, the language against ``settings.LANGUAGES`` (unknown →
    the empty auto default), and featured trophies filtered to the pilot's EARNED set before the
    cap check.
    """
    clean_name = signatures.sanitize_name(request.POST.get("name", ""))

    size_preset = request.POST.get("size_preset", "")
    layout = request.POST.get("layout", "")
    period = request.POST.get("period", "30d")
    theme = request.POST.get("theme", "gold")
    show_timestamp = request.POST.get("show_timestamp") == "1"

    bg_id = request.POST.get("background", "")
    background = None
    if bg_id.isdigit():
        background = SignatureBackground.objects.filter(pk=int(bg_id), enabled=True).first()
    if background is None:
        raise ValidationError(_("Choose an available background."))

    language = request.POST.get("language", "") or ""
    if language and language not in {code for code, _label in settings.LANGUAGES}:
        language = ""

    components = [c for c in request.POST.getlist("components") if c in signatures.COMPONENTS]

    earned = set(
        PilotTrophy.objects.filter(character_id=pilot.character_id)
        .values_list("definition_id", flat=True)
    )
    featured = []
    for raw in request.POST.getlist("featured_trophy_ids"):
        if raw.isdigit() and int(raw) in earned and int(raw) not in featured:
            featured.append(int(raw))

    config = {
        "components": components,
        "period": period,
        "featured_trophy_ids": featured,
        "show_timestamp": show_timestamp,
        "theme": theme,
    }
    clean_config = signatures.validate_config(
        config, settings=cfg, background=background, layout=layout, size_preset=size_preset
    )
    return {
        "name": clean_name,
        "background": background,
        "layout": layout,
        "size_preset": size_preset,
        "language": language,
        "config": clean_config,
    }


def _form_context(request: HttpRequest, cfg, pilot, *, sig, error=None, post=None) -> dict:
    """Assemble the builder template context for create OR edit, GET or a re-rendered failed POST.

    ``post`` (the raw QueryDict on a failed submit) is echoed so the pilot doesn't lose their input;
    on GET the values come from ``sig`` (edit) or the leadership defaults (create).
    """
    src = post if post is not None else {}
    config = (sig.config if sig else {}) or {}

    def field(name, sig_value, default=""):
        if post is not None:
            return src.get(name, default)
        return sig_value if sig is not None else default

    if post is not None:
        selected = [c for c in src.getlist("components") if c in signatures.COMPONENTS]
        featured_selected = {
            int(x) for x in src.getlist("featured_trophy_ids") if x.isdigit()
        }
    elif sig is not None:
        selected = list(config.get("components", []))
        featured_selected = set(config.get("featured_trophy_ids", []))
    else:
        selected = list(_DEFAULT_COMPONENTS)
        featured_selected = set()

    locked = bool(sig and sig.mode == CombatSignature.Mode.SNAPSHOT)

    # Layout/preset the warnings + capacity hints are computed against (echo the submit on failure).
    cur_layout = field("layout", sig.layout if sig else "", cfg.default_layout)
    cur_preset = field("size_preset", sig.size_preset if sig else "", CombatSignature.SizePreset.STANDARD)

    trophy_opts = _trophy_options(pilot.character_id)
    for opt in trophy_opts:
        opt["checked"] = opt["id"] in featured_selected

    return {
        "sig": sig,
        "editing": sig is not None,
        "config_locked": locked,
        "error_messages": list(error.messages) if error else [],
        "form": {
            "name": field("name", sig.name if sig else "", ""),
            "layout": cur_layout,
            "size_preset": cur_preset,
            "background_id": int(field("background", sig.background_id if sig else 0, 0) or 0),
            "language": field("language", sig.language if sig else "", ""),
            "period": field("period", config.get("period", "30d"), cfg.default_period),
            "theme": field("theme", config.get("theme", "gold"), "gold"),
            "show_timestamp": (
                src.get("show_timestamp") == "1" if post is not None
                else bool(config.get("show_timestamp", False))
            ),
        },
        "layout_choices": CombatSignature.Layout.choices,
        "size_preset_choices": _size_preset_choices(cfg),
        "period_choices": _period_choices(),
        "theme_choices": _theme_choices(),
        "backgrounds": _background_options(),
        "languages": settings.LANGUAGES,
        "component_items": _component_items(selected),
        "trophy_options": trophy_opts,
        "max_featured": cfg.max_featured_trophies,
        "warnings": _overflow_warnings(
            {"components": selected}, cur_layout, cur_preset
        ),
        "preview_url": reverse("killboard:signature_preview"),
    }


def _render_form(request, cfg, pilot, *, sig, error=None, post=None, status=200) -> HttpResponse:
    ctx = _form_context(request, cfg, pilot, sig=sig, error=error, post=post)
    return render(request, "killboard/signatures/form.html", ctx, status=status)


@login_required
def signature_create(request: HttpRequest) -> HttpResponse:
    """Create a new (LIVE) signature — snapshots are made by converting a live one afterwards."""
    pilot, cfg = _gate(request)
    if request.method != "POST":
        return _render_form(request, cfg, pilot, sig=None)
    try:
        parsed = _parse_builder(request, cfg, pilot)
        sig = signatures.create_signature(
            request.user, name=parsed["name"], background=parsed["background"],
            layout=parsed["layout"], size_preset=parsed["size_preset"],
            config=parsed["config"], language=parsed["language"], ip=client_ip(request),
        )
    except ValidationError as exc:
        return _render_form(request, cfg, pilot, sig=None, error=exc, post=request.POST, status=400)
    # Render the first image immediately rather than waiting for the next beat tick.
    tasks.signature_render_task.delay(sig.pk)
    _warn_dropped(request, parsed["config"], parsed["layout"], parsed["size_preset"])
    messages.success(request, _("Combat Signature created."))
    return redirect("killboard:signatures")


@login_required
def signature_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit a signature. A live signature edits its full config (bumping the render); a snapshot's
    config is frozen, so only its name is editable (A16 snapshot policy)."""
    pilot, cfg = _gate(request)
    sig = _owned_signature(pilot, pk)
    locked = sig.mode == CombatSignature.Mode.SNAPSHOT

    if request.method != "POST":
        return _render_form(request, cfg, pilot, sig=sig)

    if locked:
        # Snapshot: rename only — its configuration is pinned to the moment it was frozen.
        try:
            signatures.rename_signature(
                request.user, sig, name=request.POST.get("name", ""), ip=client_ip(request)
            )
        except ValidationError as exc:
            return _render_form(request, cfg, pilot, sig=sig, error=exc, post=request.POST,
                                status=400)
        messages.success(request, _("Snapshot renamed."))
        return redirect("killboard:signatures")

    try:
        parsed = _parse_builder(request, cfg, pilot)
        signatures.update_signature(
            request.user, sig, name=parsed["name"], background=parsed["background"],
            layout=parsed["layout"], size_preset=parsed["size_preset"],
            config=parsed["config"], language=parsed["language"], ip=client_ip(request),
        )
    except ValidationError as exc:
        return _render_form(request, cfg, pilot, sig=sig, error=exc, post=request.POST, status=400)
    tasks.signature_render_task.delay(sig.pk)
    _warn_dropped(request, parsed["config"], parsed["layout"], parsed["size_preset"])
    messages.success(request, _("Combat Signature updated."))
    return redirect("killboard:signatures")


# --------------------------------------------------------------------------- #
#  Synchronous preview (owner-only, unsaved instance, rate-limited)
# --------------------------------------------------------------------------- #
def _preview_rate() -> int:
    return int(getattr(settings, "SIGNATURE_PREVIEW_RATE", 10))


def _preview_throttle_ok(user_id) -> bool:
    """A per-user fixed window (killcard.throttle_ok shape): the TTL is set once by ``add`` and not
    refreshed by ``incr`` so the window genuinely closes; ``<=0`` disables it."""
    limit = _preview_rate()
    if limit <= 0:
        return True
    key = f"kb:sig:preview:throttle:{user_id or 'anon'}"
    cache.add(key, 0, 60)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.add(key, 1, 60)
        count = 1
    return count <= limit


def _regenerate_rate() -> int:
    return int(getattr(settings, "SIGNATURE_REGENERATE_RATE", 5))


def _regenerate_throttle_ok(user_id) -> bool:
    """A per-user fixed window for the manual regenerate action (killcard.throttle_ok shape). Each
    regenerate force-renders (clears the debounce + failure ledger), so this is the one force-render
    trigger a pilot can fire unboundedly — the rest are quota/state-bounded. ``<=0`` disables it."""
    limit = _regenerate_rate()
    if limit <= 0:
        return True
    key = f"kb:sig:regen:throttle:{user_id or 'anon'}"
    cache.add(key, 0, 60)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.add(key, 1, 60)
        count = 1
    return count <= limit


@login_required
@require_POST
def signature_preview(request: HttpRequest) -> HttpResponse:
    """Owner-only synchronous banner preview: validate the submitted builder form WITHOUT saving,
    build the payload for the acting pilot on an UNSAVED instance, and return the PNG (no-store).

    Rate-limited per user id (``SIGNATURE_PREVIEW_RATE``) → 429. CSRF applies (it's the builder
    form's POST). An invalid config returns a plain 400 with the validation message (the preview
    opens in a new tab, so a short text body is the clearest surface).
    """
    pilot, cfg = _gate(request)
    if not _preview_throttle_ok(request.user.pk):
        resp = HttpResponse(status=429)
        resp["Retry-After"] = "60"
        return resp
    try:
        parsed = _parse_builder(request, cfg, pilot)
    except ValidationError as exc:
        return HttpResponseBadRequest("; ".join(exc.messages))

    # An UNSAVED instance carries exactly what build_signature_payload reads (character + config +
    # background/layout/preset/language); nothing is written to the database.
    unsaved = CombatSignature(
        character=pilot, name=parsed["name"], background=parsed["background"],
        layout=parsed["layout"], size_preset=parsed["size_preset"],
        language=parsed["language"], mode=CombatSignature.Mode.LIVE, config=parsed["config"],
    )
    # The preview fetches the real portrait/logos so it reflects the published image, but with a
    # short timeout so a cold fetch can't hold a gunicorn thread (the per-user preview throttle
    # bounds abuse, and each asset is tiny and cached for a week after the first fetch). A fetch
    # that exceeds the budget falls back to the monogram exactly as the render would.
    with translation.override(translation.get_language()):
        payload = build_signature_payload(unsaved, fetch_timeout=_PREVIEW_FETCH_TIMEOUT)
    png = render_signature_png(unsaved, payload)
    resp = HttpResponse(png, content_type="image/png")
    resp["Cache-Control"] = "no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


# --------------------------------------------------------------------------- #
#  Actions (POST-only, one endpoint, owner-gated in the domain layer)
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def signature_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    """A single POST-action endpoint dispatching to the owner-gated domain mutations.

    Destructive verbs (delete / rotate / disable / snapshot) carry ``data-confirm`` on their form in
    the template. Every verb redirects back to the list with a Django flash message; a domain
    ``ValidationError`` (e.g. duplicate at quota, snapshots disabled) surfaces as an error message.
    """
    pilot, _cfg = _gate(request)
    if action not in _ACTIONS:
        raise Http404(_("Unknown action."))
    sig = _owned_signature(pilot, pk)
    try:
        _dispatch_action(request, sig, action, client_ip(request))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("killboard:signatures")


def _dispatch_action(request: HttpRequest, sig: CombatSignature, action: str, ip: str) -> None:
    user = request.user
    if action == "regenerate":
        if not _regenerate_throttle_ok(user.pk):
            messages.error(
                request,
                _("You're regenerating too quickly — please wait a minute and try again."),
            )
            return
        tasks.signature_render_task.delay(sig.pk)
        audit_log(
            user, "signatures.regenerate", target_type="combat_signature",
            target_id=str(sig.pk), metadata={"character_id": sig.character_id}, ip=ip,
        )
        messages.success(request, _("Re-render queued."))
    elif action == "duplicate":
        copy = signatures.duplicate_signature(user, sig, ip=ip)
        tasks.signature_render_task.delay(copy.pk)
        messages.success(request, _("Signature duplicated."))
    elif action == "snapshot":
        signatures.take_snapshot(user, sig, ip=ip)
        tasks.signature_render_task.delay(sig.pk)
        messages.success(request, _("Converted to a frozen snapshot."))
    elif action == "rotate":
        signatures.rotate_token(user, sig, ip=ip)
        tasks.signature_render_task.delay(sig.pk)
        messages.success(request, _("Public URL rotated — the old link no longer works."))
    elif action == "disable":
        signatures.disable(user, sig, ip=ip)
        messages.success(request, _("Signature disabled."))
    elif action == "enable":
        signatures.enable(user, sig, ip=ip)
        tasks.signature_render_task.delay(sig.pk)
        messages.success(request, _("Signature enabled."))
    elif action == "delete":
        signatures.delete_signature(user, sig, ip=ip)
        messages.success(request, _("Signature deleted."))
