"""Combat Signatures — the payload builder (all DB + i18n work; the renderer stays pure).

``build_signature_payload(signature)`` composes exactly the data a signature's *selected*
components need — and nothing else — into a plain dict the pure :mod:`signature_render` kernel
draws. Two invariants shape this module:

* **Privacy by construction (plan A6).** A stat, an identity field or a trophy is queried and
  carried ONLY when the signature's config selects its component. An unselected component leaves
  no key in the payload — the renderer can never draw, and a leak can never expose, data the pilot
  did not choose to publish.
* **All localisation happens here.** Every human-visible label is built under
  ``translation.override(lang)`` (``lang`` = the signature's pinned language, else the owner's
  resolved locale, else the corp broadcast locale) following the pingboard fan-out precedent, so
  the render step needs no locale context. EVE proper nouns (pilot / corp / ship names) are carried
  RAW; rank titles come through the ``ranks_i18n`` render-time seam; compact ISK is the shared,
  deliberately locale-neutral formatter.

The authoritative stat sources are reused verbatim (no parallel aggregation): the ``leaderboards``
window helpers for the period card, ``ranks`` for lifetime rank + progress, ``trophies`` for earned
trophies, and the ``cv`` derivations for best kill / favourite hull. Portrait and logo files are
resolved through the worker-side mirror (:mod:`signature_assets`) as a pre-step — the only network
this feature ever does, and never on a render request.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from . import ranks, trophies
from . import signature_assets as assets
from .imagekit import compact_isk

# Components whose value comes from the period combat card — the card is built once iff any is set.
_CARD_COMPONENTS = frozenset({
    "kills", "losses", "solo_kills", "final_blows",
    "isk_destroyed", "isk_lost", "isk_efficiency", "kd_ratio",
})


def _home() -> int:
    return settings.FORCA_HOME_CORP_ID


# --------------------------------------------------------------------------- #
#  Language resolution (request-less; the pingboard precedent for a Celery task).
# --------------------------------------------------------------------------- #
def _resolve_language(signature) -> str:
    """The locale a signature renders in: its pinned language, else the owner's, else broadcast.

    A pilot's explicit per-signature pin (chosen from the full translated locale set in the
    builder) is honoured against every supported LANGUAGE — a banner is a deliberate artefact, not
    the UI, so a locale leadership merely hides from the selector should not silently override it.
    The owner-preference and broadcast fallbacks resolve against the offered (enabled) locales, as
    ``core.i18n.resolver`` does for a request. Anything unknown/malformed is skipped.
    """
    from django.utils.translation import get_supported_language_variant

    from core.i18n.config import broadcast_locale, enabled_locales

    def norm(code):
        if not code or not isinstance(code, str):
            return None
        try:
            return get_supported_language_variant(code.replace("_", "-"))
        except (LookupError, TypeError, ValueError):
            return None

    supported = {code for code, _label in settings.LANGUAGES}
    pin = norm(signature.language)
    if pin and pin in supported:
        return pin

    enabled = set(enabled_locales())
    owner_lang = ""
    user = getattr(signature.character, "user", None)
    if user is not None:
        owner_lang = getattr(user, "language", "") or ""
    for candidate in (owner_lang, broadcast_locale()):
        hit = norm(candidate)
        if hit and hit in enabled:
            return hit
    return "en"


# --------------------------------------------------------------------------- #
#  Small name resolvers (single-row SDE reads).
# --------------------------------------------------------------------------- #
def _type_name(type_id) -> str:
    if not type_id:
        return ""
    from apps.sde.models import SdeType

    return (
        SdeType.objects.filter(type_id=type_id).values_list("name", flat=True).first()
        or f"Type {type_id}"
    )


def _ship_class(type_id) -> str:
    if not type_id:
        return ""
    from apps.sde.models import SdeGroup, SdeType

    gid = SdeType.objects.filter(type_id=type_id).values_list("group_id", flat=True).first()
    if not gid:
        return ""
    return SdeGroup.objects.filter(group_id=gid).values_list("name", flat=True).first() or ""


# --------------------------------------------------------------------------- #
#  Stat sources.
# --------------------------------------------------------------------------- #
def _period_card(character_id: int, period: str) -> dict:
    """The pilot's combat card for one activity window, via the leaderboards helpers.

    Uses the same private per-window aggregates the all-time card is built from, so a signature's
    numbers exactly match the rankings for that window (one kill-row + one loss-row read).
    """
    from . import leaderboards as lb

    window = lb.window_for(period)
    # ``.order_by`` is required before ``.first()`` on these GROUP BY aggregates (the same shape
    # ``leaderboards._card_live`` uses).
    kr = (
        lb._kill_rows(window).filter(character_id=character_id)
        .order_by("character_id").first() or {}
    )
    lr = (
        lb._loss_rows(window).filter(victim_character_id=character_id)
        .order_by("victim_character_id").first() or {}
    )
    return lb._card_from(
        character_id,
        kills=kr.get("kills", 0) or 0,
        losses=lr.get("losses", 0) or 0,
        solo_kills=kr.get("solo_kills", 0) or 0,
        final_blows=kr.get("final_blows", 0) or 0,
        points=kr.get("points", 0) or 0,
        isk_destroyed=kr.get("isk_destroyed", 0) or 0,
        isk_lost=lr.get("isk_lost", 0) or 0,
    )


def _last_kill(character_id: int) -> dict | None:
    """The pilot's most recent home kill (ship + time) — derived, no CV helper exists for it."""
    from .models import Killmail, KillmailParticipant

    row = (
        KillmailParticipant.objects.filter(
            role=KillmailParticipant.Role.ATTACKER, corporation_id=_home(),
            character_id=character_id,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER, killmail__is_npc=False,
        )
        .order_by("-killmail__killmail_time")
        .values("killmail__victim_ship_type_id", "killmail__killmail_time")
        .first()
    )
    if not row:
        return None
    return {
        "victim_ship_type_id": row["killmail__victim_ship_type_id"],
        "killmail_time": row["killmail__killmail_time"],
    }


def _featured_trophies(character_id: int, featured_ids, limit: int) -> list[dict]:
    """The pilot's featured trophies — the config ids filtered to those ACTUALLY earned.

    A pilot can never surface a trophy they have not earned (an id they never won, or one revoked
    since, simply drops out), and the result preserves the config's declared order.
    """
    if not featured_ids:
        return []
    from .models import PilotTrophy

    earned = {
        pt.definition_id: pt
        for pt in PilotTrophy.objects.filter(
            character_id=character_id, definition_id__in=featured_ids
        ).select_related("definition")
    }
    out = []
    for did in featured_ids:
        pt = earned.get(did)
        if pt is None:
            continue
        out.append({
            "name": pt.definition.name,          # RAW — a trophy name is an EVE-flavoured noun
            "tier": pt.definition.tier,          # bronze / silver / gold → drawn medal colour
            "color": pt.definition.color_class,
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
#  Payload builder.
# --------------------------------------------------------------------------- #
def build_signature_payload(signature, *, fetch_assets: bool = True) -> dict:
    """Compose the render payload for ``signature`` — selected components only, fully localised.

    ``fetch_assets`` gates the worker-side portrait/logo mirror fetch (the only network this
    feature performs). It defaults on for the real Celery pre-step; tests that assert pure payload
    shape pass ``fetch_assets=False`` so no HTTP happens.

    The returned dict carries the render-driving config mirror (``size_preset`` / ``layout`` /
    ``theme`` / ``components`` / ``background_key`` / ``show_timestamp``), ``language``,
    ``generated_at``, a ``labels`` sub-dict (localised, selected components only) and one data key
    per selected component. Nothing else.
    """
    config = signature.config or {}
    comps: list[str] = list(config.get("components", []))
    comp_set = set(comps)
    theme = config.get("theme", "gold")
    period = config.get("period", "30d")
    show_timestamp = bool(config.get("show_timestamp", False))
    lang = _resolve_language(signature)
    character = signature.character
    cid = character.character_id

    payload: dict = {
        "signature_id": signature.pk,
        "background_key": signature.background.key,
        "size_preset": signature.size_preset,
        "layout": signature.layout,
        "theme": theme,
        "components": comps,
        "show_timestamp": show_timestamp,
        "language": lang,
        "generated_at": timezone.now(),
        "labels": {},
    }
    labels: dict[str, str] = payload["labels"]

    with translation.override(lang):
        # --- identity ----------------------------------------------------- #
        pilot_name = character.name or f"Pilot {cid}"
        if "portrait" in comp_set:
            from .imagekit import monogram
            payload["portrait"] = {
                "path": assets.ensure_portrait(cid) if fetch_assets else None,
                "monogram": monogram(pilot_name),
            }
        if "pilot_name" in comp_set:
            payload["pilot_name"] = pilot_name          # RAW EVE proper noun

        if "corp" in comp_set:
            corp = character.corporation
            if corp is not None:
                payload["corp"] = {
                    "id": corp.corporation_id,
                    "name": corp.name or "",
                    "ticker": corp.ticker or "",
                    "logo_path": (
                        assets.ensure_corp_logo(corp.corporation_id) if fetch_assets else None
                    ),
                }
                labels["corp"] = _("Corp")

        if "alliance" in comp_set and character.alliance_id:
            from apps.corporation.models import EveAlliance
            row = (
                EveAlliance.objects.filter(alliance_id=character.alliance_id)
                .values("alliance_id", "name", "ticker").first()
            )
            aid = row["alliance_id"] if row else character.alliance_id
            payload["alliance"] = {
                "id": aid,
                "name": (row["name"] if row else "") or "",
                "ticker": (row["ticker"] if row else "") or "",
                "logo_path": assets.ensure_alliance_logo(aid) if fetch_assets else None,
            }
            labels["alliance"] = _("Alliance")

        # --- period combat card (built once, only if a card stat is selected) --- #
        if comp_set & _CARD_COMPONENTS:
            card = _period_card(cid, period)
            if "kills" in comp_set:
                payload["kills"] = int(card["kills"])
                labels["kills"] = _("Kills")
            if "losses" in comp_set:
                payload["losses"] = int(card["losses"])
                labels["losses"] = _("Losses")
            if "solo_kills" in comp_set:
                payload["solo_kills"] = int(card["solo_kills"])
                labels["solo_kills"] = _("Solo")
            if "final_blows" in comp_set:
                payload["final_blows"] = int(card["final_blows"])
                labels["final_blows"] = _("Final blows")
            if "isk_destroyed" in comp_set:
                val = float(card["isk_destroyed"] or 0)
                payload["isk_destroyed"] = {"value": val, "text": compact_isk(val)}
                labels["isk_destroyed"] = _("ISK destroyed")
            if "isk_lost" in comp_set:
                val = float(card["isk_lost"] or 0)
                payload["isk_lost"] = {"value": val, "text": compact_isk(val)}
                labels["isk_lost"] = _("ISK lost")
            if "isk_efficiency" in comp_set:
                eff = float(card["efficiency"] or 0)
                payload["isk_efficiency"] = {"value": eff, "text": f"{eff:.1f}%"}
                labels["isk_efficiency"] = _("Efficiency")
            if "kd_ratio" in comp_set:
                k, ls = int(card["kills"]), int(card["losses"])
                ratio = (k / ls) if ls else float(k)
                payload["kd_ratio"] = {"value": ratio, "text": f"{ratio:.1f}"}
                labels["kd_ratio"] = _("K/D")

        # --- rank (lifetime, not period-scoped) --------------------------- #
        if "rank_title" in comp_set or "rank_progress" in comp_set:
            counts = ranks.pilot_metric_counts(cid)
            prog = ranks.rank_progress(counts["kills"])
            current = prog.get("current") or {}
            if "rank_title" in comp_set:
                payload["rank_title"] = current.get("title") or ""   # translated via ranks_i18n
                labels["rank_title"] = _("Rank")
            if "rank_progress" in comp_set:
                nxt = prog.get("next") or {}
                payload["rank_progress"] = {
                    "pct": float(prog.get("progress_pct") or 0.0),
                    "current_title": current.get("title") or "",
                    "next_title": nxt.get("title") or "",
                    "to_next": int(prog.get("kills_to_next") or 0),
                    "is_maxed": bool(prog.get("is_maxed")),
                }
                labels["rank_progress"] = _("Progress")

        # --- trophies ----------------------------------------------------- #
        if "trophies_featured" in comp_set:
            from .models import CombatSignatureSettings
            limit = CombatSignatureSettings.load().max_featured_trophies
            payload["trophies_featured"] = _featured_trophies(
                cid, config.get("featured_trophy_ids", []), limit
            )
            labels["trophies_featured"] = _("Trophies")
        if "trophy_count" in comp_set:
            payload["trophy_count"] = len(trophies.pilot_trophies(cid))
            labels["trophy_count"] = _("Trophies")

        # --- kills / hulls ------------------------------------------------ #
        if "last_kill" in comp_set:
            lk = _last_kill(cid)
            payload["last_kill"] = (
                {"ship_name": _type_name(lk["victim_ship_type_id"]),
                 "killmail_time": lk["killmail_time"]}
                if lk else None
            )
            labels["last_kill"] = _("Last kill")
        if "best_kill" in comp_set:
            from .cv import _best_kill
            bk = _best_kill(cid)
            payload["best_kill"] = (
                {"value": float(bk["value"] or 0), "text": compact_isk(bk["value"] or 0),
                 "ship_name": _type_name(bk["victim_ship_type_id"])}
                if bk else None
            )
            labels["best_kill"] = _("Best kill")
        if "favourite_ship" in comp_set:
            from .cv import _favourite_hull
            fh = _favourite_hull(cid)
            payload["favourite_ship"] = (
                {"ship_name": _type_name(fh["ship_type_id"]), "count": int(fh["count"])}
                if fh else None
            )
            labels["favourite_ship"] = _("Top hull")
        if "top_ship_class" in comp_set:
            from .cv import _favourite_hull
            fh = _favourite_hull(cid)
            payload["top_ship_class"] = _ship_class(fh["ship_type_id"]) if fh else None
            labels["top_ship_class"] = _("Top class")

        # --- meta strip --------------------------------------------------- #
        if "activity_period_label" in comp_set:
            from . import leaderboards as lb
            payload["activity_period_label"] = str(lb.window_for(period).label)
        if "stats_timestamp" in comp_set or show_timestamp:
            from django.utils.formats import date_format
            payload["stats_timestamp"] = date_format(
                timezone.localtime(payload["generated_at"]), "SHORT_DATETIME_FORMAT"
            )
            labels["stats_timestamp"] = _("Updated")

    return payload
