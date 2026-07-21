"""KB-30 — per-pilot subscriptions: matching, delivery and the emission hooks.

A member subscribes, self-serve, to personal killboard events and picks a channel. This
module owns everything downstream of that choice:

* **Emission architecture** — the killmail-driven event types (``my_kill`` / ``my_loss`` /
  ``my_loss_srp_pending`` / ``filter_match``) are matched by a **cursor-consumer** beat task
  (:func:`dispatch_subscriptions`) that walks the KB-29 ``KillboardStreamEvent`` ring buffer
  by its monotonic ``seq`` — the same cursor contract the outbound stream serves. That reuses
  the ring buffer's denormalised topic dimensions (so a match needs no ``Killmail`` join),
  inherits its "fresh, home-corp only" emission (a years-deep EVE Ref backfill can never fire
  a subscription), and is naturally batched and resumable — strictly cheaper and cleaner than
  piggybacking a fan-out onto every single ingest. ``rank_up`` and ``watchlist_hit`` are NOT
  killmail-stream events, so they are pushed from the EXISTING ``rank_notify`` /
  ``watchlist_alerts`` emission points (:func:`notify_user_event` / :func:`notify_watchlist_hit`)
  — never re-derived here.

* **Delivery** — per channel, best-effort and isolated (one bad send never breaks the sweep):
  ``notify`` rides Pingboard's per-user route, ``email`` uses Django's ``EMAIL_BACKEND``,
  ``webhook`` is an SSRF-guarded HTTPS POST with one retry and a dead-letter auto-disable, and
  ``rss`` is pull-only (the stored feed row is the content, rendered at read time).

* **RBAC** — a subscription only ever delivers what its owning member could see on the board:
  ``my_*`` resolve against the user's own linked pilots, every payload is the member-visible
  public stream shape, and delivery re-checks the owner still holds membership.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import types
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone, translation
from django.utils.translation import gettext

from core import rbac

from . import killfeed_rules, stream
from .models import (
    KillboardStreamEvent,
    KillboardSubscription,
    KillboardSubscriptionEvent,
    Killmail,
    SubscriptionChannel,
    SubscriptionEventType,
)

log = logging.getLogger("forca.killboard")

_KILLMAIL_EVENT_VALUES = [e for e in KillboardSubscription.KILLMAIL_EVENT_TYPES]


# --------------------------------------------------------------------------- #
#  Settings accessors (all env-overridable; see config/settings/base.py)
# --------------------------------------------------------------------------- #
def _enabled() -> bool:
    return bool(getattr(settings, "KILLBOARD_SUBSCRIPTIONS_ENABLED", True))


def per_user_cap() -> int:
    return int(getattr(settings, "KILLBOARD_SUBSCRIPTIONS_PER_USER_CAP", 20))


def _webhook_timeout() -> float:
    return float(getattr(settings, "KILLBOARD_SUBSCRIPTION_WEBHOOK_TIMEOUT_S", 5))


def _webhook_retries() -> int:
    return int(getattr(settings, "KILLBOARD_SUBSCRIPTION_WEBHOOK_RETRIES", 1))


def _webhook_max_failures() -> int:
    return int(getattr(settings, "KILLBOARD_SUBSCRIPTION_WEBHOOK_MAX_FAILURES", 5))


def _feed_keep() -> int:
    return int(getattr(settings, "KILLBOARD_SUBSCRIPTION_FEED_KEEP", 100))


def _batch() -> int:
    return int(getattr(settings, "KILLBOARD_SUBSCRIPTION_BATCH", 500))


# --------------------------------------------------------------------------- #
#  SSRF guard for user-supplied webhook targets
# --------------------------------------------------------------------------- #
# A user picks an arbitrary webhook URL, so an allowlist is impossible; instead we DENY any
# target that resolves to a non-public address. Checked at SAVE (form clean) and again at
# SEND (a hostname can be re-pointed at a private IP after it was saved — DNS rebinding), and
# redirects are refused so a 3xx can't bounce the POST to an internal host. Residual note:
# `requests` re-resolves the host at connect time, so a name that flips between the send-time
# check and the socket connect is the one gap this can't fully close without pinning the
# resolved IP; the save+send checks plus no-redirects stop the static and rebind-on-save cases.


def _addr_blocked(ip_str: str) -> bool:
    """True when a resolved address is loopback / private / link-local / otherwise non-public."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable — refuse rather than guess
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # an IPv4-mapped IPv6 (::ffff:127.0.0.1) hides the real target
    return bool(
        ip.is_private          # RFC1918 (10/8, 172.16/12, 192.168/16) + friends
        or ip.is_loopback      # 127/8, ::1
        or ip.is_link_local    # 169.254/16 (incl. the 169.254.169.254 metadata endpoint), fe80::/10
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified   # 0.0.0.0, ::
    )


def webhook_url_error(url: str) -> str | None:
    """A human error string when ``url`` is not a safe https webhook target, else ``None``.

    Enforced identically on save and on send. Resolves every address the host maps to and
    blocks the delivery if ANY of them is non-public, so a hostname with one private A record
    can't smuggle a request onto the internal network.
    """
    if not url:
        return gettext("A webhook URL is required.")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return gettext("The webhook URL must use https.")
    host = parsed.hostname
    if not host:
        return gettext("The webhook URL has no host.")
    try:
        port = parsed.port or 443
    except ValueError:
        return gettext("The webhook URL has an invalid port.")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return gettext("The webhook host could not be resolved.")
    except (UnicodeError, ValueError):
        return gettext("The webhook host is not valid.")
    for info in infos:
        if _addr_blocked(info[4][0]):
            return gettext("The webhook host resolves to a private, loopback or reserved address.")
    return None


# --------------------------------------------------------------------------- #
#  Saved-filter evaluation (reuses the KB-24 killfeed_rules clause engine)
# --------------------------------------------------------------------------- #
# A killfeed_rules-style clause dict evaluated against a stream event. The engine reads a
# small set of cfg attributes and a killmail-like object; we synthesise both from the ring
# buffer row (which denormalises every dimension the engine needs — including a `deviated`
# boolean we re-expose as a fit_deviation stand-in), so a filter match needs no Killmail load.
_FILTER_CLAUSE_KEYS = (
    "min_loss_value", "min_kill_value", "exclude_npc", "exclude_awox", "require_solo",
    "min_attackers", "max_attackers", "sec_bands", "ship_classes",
    "max_jumps_from_staging", "losses_deviated_only",
)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def filter_cfg(clause: dict):
    """Build a ``KillFeedConfig``-shaped namespace from a saved clause dict."""
    clause = clause or {}
    return types.SimpleNamespace(
        min_loss_value=_decimal(clause.get("min_loss_value", 0)),
        min_kill_value=_decimal(clause.get("min_kill_value", 0)),
        exclude_npc=bool(clause.get("exclude_npc", False)),
        exclude_awox=bool(clause.get("exclude_awox", False)),
        require_solo=bool(clause.get("require_solo", False)),
        min_attackers=int(clause.get("min_attackers", 0) or 0),
        max_attackers=int(clause.get("max_attackers", 0) or 0),
        sec_bands=list(clause.get("sec_bands", []) or []),
        ship_classes=list(clause.get("ship_classes", []) or []),
        max_jumps_from_staging=int(clause.get("max_jumps_from_staging", 0) or 0),
        losses_deviated_only=bool(clause.get("losses_deviated_only", False)),
    )


def _km_view(ev: KillboardStreamEvent):
    """A ``Killmail``-shaped view over a stream event, for ``killfeed_rules.evaluate``."""
    is_loss = ev.home_role == Killmail.HomeRole.VICTIM
    view = types.SimpleNamespace(
        home_corp_role=ev.home_role,
        total_value=ev.total_value,
        is_npc=ev.is_npc,
        is_awox=ev.is_awox,
        is_solo=ev.is_solo,
        sec_band=ev.sec_band,
        solar_system_id=ev.system_id,
        # The engine reads getattr(km, "fit_deviation", None).is_clean; the event's precomputed
        # `deviated` boolean is exactly that verdict, so re-expose it as a stand-in.
        fit_deviation=(types.SimpleNamespace(is_clean=not ev.deviated) if is_loss else None),
    )
    return view


def clause_is_meaningful(clause: dict) -> bool:
    """True when a filter clause would ever match anything.

    With both ISK floors at 0 the engine mutes both directions (the killfeed "0 = off"
    contract), so such a clause is inert — the form uses this to reject a no-op filter.
    """
    cfg = filter_cfg(clause)
    return cfg.min_kill_value > 0 or cfg.min_loss_value > 0


def _filter_matches(clause: dict, ev: KillboardStreamEvent, *, attacker_count: int, staging) -> bool:
    cfg = filter_cfg(clause)
    return killfeed_rules.evaluate(
        _km_view(ev), cfg,
        attacker_count=attacker_count, ship_class=ev.ship_class or "Other",
        staging_distance=staging,
    )


# --------------------------------------------------------------------------- #
#  Name resolution (best-effort, from our own caches — no ESI in the sweep)
# --------------------------------------------------------------------------- #
def _resolve_names(entity_ids, type_ids, system_ids) -> dict[int, str]:
    from apps.corporation.models import EveName
    from apps.sde.models import SdeSolarSystem, SdeType

    names: dict[int, str] = {}
    entity_ids = {int(i) for i in entity_ids if i}
    type_ids = {int(i) for i in type_ids if i}
    system_ids = {int(i) for i in system_ids if i}
    if entity_ids:
        names.update(EveName.objects.filter(entity_id__in=entity_ids).values_list("entity_id", "name"))
    if type_ids:
        names.update(SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name"))
    if system_ids:
        for sid, name in SdeSolarSystem.objects.filter(system_id__in=system_ids).values_list("system_id", "name"):
            names.setdefault(sid, name)
    return names


# --------------------------------------------------------------------------- #
#  Rendering — one matched event → (title, summary, link, payload)
# --------------------------------------------------------------------------- #
def _km_link(killmail_id) -> str:
    return f"/killboard/{killmail_id}/" if killmail_id else ""


def render_stream_event(event_type: str, ev: KillboardStreamEvent, names: dict) -> dict:
    """Human title/summary/link + the member-visible public payload for a killmail event."""
    ship = names.get(ev.victim_ship_type_id) or gettext("a ship")
    system = names.get(ev.system_id) or gettext("unknown space")
    victim = names.get(ev.victim_character_id) or gettext("someone")
    value = f"{ev.total_value:,.0f}" if ev.total_value is not None else "0"
    if event_type == SubscriptionEventType.MY_KILL:
        title = gettext("Kill: %(victim)s (%(ship)s) in %(system)s") % {
            "victim": victim, "ship": ship, "system": system}
    elif event_type == SubscriptionEventType.MY_LOSS:
        title = gettext("Loss: your %(ship)s in %(system)s") % {"ship": ship, "system": system}
    elif event_type == SubscriptionEventType.MY_LOSS_SRP_PENDING:
        title = gettext("SRP eligible: your %(ship)s loss in %(system)s") % {
            "ship": ship, "system": system}
    else:  # filter_match
        title = gettext("Filter match: %(ship)s in %(system)s") % {"ship": ship, "system": system}
    summary = gettext("%(value)s ISK · %(system)s") % {"value": value, "system": system}
    return {
        "title": title[:200],
        "summary": summary[:500],
        "link": _km_link(ev.killmail_id),
        "payload": stream.event_payload(ev, member=True),
        "killmail_id": ev.killmail_id,
        "seq": ev.seq,
    }


# --------------------------------------------------------------------------- #
#  Delivery — per channel, best-effort and isolated
# --------------------------------------------------------------------------- #
# Pingboard channels the per-user `notify` route delivers over: the always-on in-app inbox
# plus verified chat DM handles. Discord (a broadcast webhook) and EVE-mail are deliberately
# excluded — the former can't DM an individual today, the latter would spam in-game mail.
_NOTIFY_CHANNELS = ["in_app", "slack", "telegram", "whatsapp"]


def _user_lang(user) -> str:
    from core.i18n import broadcast_locale

    return getattr(user, "language", "") or broadcast_locale()


def _deliver_notify(sub: KillboardSubscription, item: KillboardSubscriptionEvent) -> tuple[bool, str]:
    from apps.pingboard import services as pingboard

    try:
        alert = pingboard.emit_broadcast(
            category="custom",
            title=item.title,
            body=item.summary or item.title,
            channels=_NOTIFY_CHANNELS,
            audience={"kind": "user", "id": sub.user_id},
            source_service="killboard",
            source_object_id=f"kbsub:{sub.id}",
            idempotency_key=f"kbsub:{sub.id}:{item.id}",
        )
    except Exception as exc:  # noqa: BLE001 — a notification hiccup never breaks the sweep
        return False, f"notify failed: {type(exc).__name__}"
    return (alert is not None), ("" if alert is not None else "notification suppressed")


def _deliver_email(sub: KillboardSubscription, item: KillboardSubscriptionEvent) -> tuple[bool, str]:
    from django.core.mail import send_mail

    to = (getattr(sub.user, "email", "") or "").strip()
    if not to:
        return False, "no email address on file"
    body = item.summary or item.title
    if item.link:
        body = f"{body}\n\nhttps://{_site_host()}{item.link}"
    try:
        with translation.override(_user_lang(sub.user)):
            sent = send_mail(
                subject=item.title,
                message=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[to],
                fail_silently=False,
            )
    except Exception as exc:  # noqa: BLE001 — a mail transport error must not break the sweep
        return False, f"email failed: {type(exc).__name__}"
    return (sent > 0), ("" if sent > 0 else "no email sent")


def _site_host() -> str:
    hosts = getattr(settings, "ALLOWED_HOSTS", []) or []
    for h in hosts:
        if h and h not in ("*", "localhost", "127.0.0.1"):
            return h
    return "localhost"


def _deliver_webhook(sub: KillboardSubscription, item: KillboardSubscriptionEvent) -> tuple[bool, str]:
    import requests

    err = webhook_url_error(sub.webhook_url)  # re-validate at send (DNS rebinding / SSRF)
    if err:
        return False, str(err)
    body = {
        "event_type": item.event_type,
        "title": item.title,
        "summary": item.summary,
        "link": item.link,
        "killmail_id": item.killmail_id,
        "data": item.payload,  # the member-visible public stream shape — no sensitive fields
    }
    timeout = _webhook_timeout()
    last_err = "no attempt"
    for _attempt in range(1 + max(0, _webhook_retries())):
        try:
            resp = requests.post(
                sub.webhook_url, json=body, timeout=timeout, allow_redirects=False,
                headers={"User-Agent": "forca-killboard/1.0"},
            )
        except requests.RequestException as exc:
            last_err = f"request failed: {type(exc).__name__}"
            continue
        if 200 <= resp.status_code < 300:
            return True, ""
        last_err = f"http {resp.status_code}"
    return False, last_err


def _deliver(sub: KillboardSubscription, item: KillboardSubscriptionEvent) -> tuple[bool, str]:
    """Route a recorded feed item to its channel. RSS is pull-only — nothing is pushed."""
    if sub.channel == SubscriptionChannel.RSS:
        return True, ""  # the stored item IS the feed; it is served at read time
    if sub.channel == SubscriptionChannel.NOTIFY:
        return _deliver_notify(sub, item)
    if sub.channel == SubscriptionChannel.EMAIL:
        return _deliver_email(sub, item)
    if sub.channel == SubscriptionChannel.WEBHOOK:
        return _deliver_webhook(sub, item)
    return False, "unknown channel"


def _record_result(sub: KillboardSubscription, ok: bool, err: str) -> None:
    """Stamp last_fired on success; on webhook failure, count toward the dead-letter ceiling."""
    fields = ["updated_at"]
    if ok:
        if sub.consecutive_failures:
            sub.consecutive_failures = 0
            fields.append("consecutive_failures")
        sub.last_fired = timezone.now()
        fields.append("last_fired")
    else:
        sub.consecutive_failures += 1
        fields.append("consecutive_failures")
        if sub.channel == SubscriptionChannel.WEBHOOK and sub.consecutive_failures >= _webhook_max_failures():
            sub.enabled = False
            sub.disabled_reason = gettext(
                "Auto-disabled after %(n)d consecutive delivery failures."
            ) % {"n": sub.consecutive_failures}
            fields += ["enabled", "disabled_reason"]
        log.info("kb subscription %s delivery failed (%s): %s", sub.id, sub.channel, err)
    sub.save(update_fields=list(dict.fromkeys(fields)))


def _prune_feed(sub: KillboardSubscription) -> None:
    """Keep only the newest ``_feed_keep`` feed rows for a subscription (bounds the table + RSS)."""
    keep = _feed_keep()
    cutoff = (
        KillboardSubscriptionEvent.objects.filter(subscription=sub)
        .order_by("-created", "-id").values_list("id", flat=True)[keep : keep + 1].first()
    )
    if cutoff is not None:
        KillboardSubscriptionEvent.objects.filter(subscription=sub, id__lte=cutoff).delete()


def record_and_deliver(sub: KillboardSubscription, rendered: dict) -> bool:
    """Record a matched event against ``sub`` and deliver it. Returns True on delivery success.

    Never raises — a bad single delivery is logged and returns False so the caller's sweep
    continues. ``rendered`` is the dict from :func:`render_stream_event` (or an equivalent for
    the pushed rank_up / watchlist_hit events).
    """
    try:
        item = KillboardSubscriptionEvent.objects.create(
            subscription=sub,
            event_type=sub.event_type,
            killmail_id=rendered.get("killmail_id"),
            seq=rendered.get("seq"),
            title=rendered.get("title", "")[:200],
            summary=(rendered.get("summary") or "")[:500],
            link=(rendered.get("link") or "")[:300],
            payload=rendered.get("payload") or {},
        )
        ok, err = _deliver(sub, item)
        _record_result(sub, ok, err)
        _prune_feed(sub)
        return ok
    except Exception:  # noqa: BLE001 — one subscription must never sink the whole sweep
        log.exception("kb subscription %s record/deliver failed", getattr(sub, "id", "?"))
        return False


# --------------------------------------------------------------------------- #
#  Membership gate (a subscription only ever delivers member-tier data)
# --------------------------------------------------------------------------- #
def _member_ok(user, cache: dict) -> bool:
    key = getattr(user, "id", None)
    if key in cache:
        return cache[key]
    ok = bool(user) and rbac.has_role(user, rbac.ROLE_MEMBER)
    cache[key] = ok
    return ok


# --------------------------------------------------------------------------- #
#  Cursor-consumer — the killmail-driven event sweep (a beat task)
# --------------------------------------------------------------------------- #
def dispatch_subscriptions() -> dict:
    """Match fresh stream events against enabled killmail-driven subscriptions and deliver.

    Walks ``KillboardStreamEvent`` by ``seq`` from the lowest per-subscription cursor, matches
    each enabled ``my_*`` / ``filter_match`` subscription, and advances every scanned
    subscription's cursor past the batch (so a filtered-out event never stalls resume). No-op
    when the feature is off or nothing new has landed.
    """
    if not _enabled():
        return {"status": "disabled"}

    subs = list(
        KillboardSubscription.objects.filter(
            enabled=True, event_type__in=_KILLMAIL_EVENT_VALUES
        ).select_related("user")
    )
    if not subs:
        return {"status": "ok", "matched": 0, "subscriptions": 0}

    tip = stream.tip_seq()
    floor = min(s.last_seq for s in subs)
    if floor >= tip:
        return {"status": "ok", "matched": 0, "subscriptions": len(subs)}

    batch = list(
        KillboardStreamEvent.objects.filter(seq__gt=floor).order_by("seq")[: _batch()]
    )
    if not batch:
        # The buffer was pruned past the floor; fast-forward every cursor to the tip.
        KillboardSubscription.objects.filter(
            id__in=[s.id for s in subs], last_seq__lt=tip
        ).update(last_seq=tip)
        return {"status": "ok", "matched": 0, "subscriptions": len(subs)}
    processed_tip = batch[-1].seq

    wants_kill = any(s.event_type == SubscriptionEventType.MY_KILL for s in subs)
    wants_filter = any(s.event_type == SubscriptionEventType.FILTER_MATCH for s in subs)
    attackers_by_km, count_by_km = _attacker_maps(batch) if (wants_kill or wants_filter) else ({}, {})
    staging = _staging_for(subs)
    names = _batch_names(batch)

    member_cache: dict = {}
    char_cache: dict = {}
    matched = 0
    for sub in subs:
        if not _member_ok(sub.user, member_cache):
            sub.last_seq = processed_tip
            sub.save(update_fields=["last_seq", "updated_at"])
            continue
        my_char_ids = _char_ids(sub.user, char_cache)
        # Render each matched event in the recipient's own language (worker-safe override —
        # there is no LocaleMiddleware here). EVE proper names in `names` are locale-independent.
        with translation.override(_user_lang(sub.user)):
            for ev in batch:
                if ev.seq <= sub.last_seq:
                    continue
                if _matches(sub, ev, my_char_ids=my_char_ids, attackers_by_km=attackers_by_km,
                            count_by_km=count_by_km, staging=staging):
                    if record_and_deliver(sub, render_stream_event(sub.event_type, ev, names)):
                        matched += 1
        # Advance the cursor past the whole scanned batch (matched or not), then persist.
        if sub.last_seq != processed_tip:
            sub.last_seq = processed_tip
            sub.save(update_fields=["last_seq", "updated_at"])
    return {"status": "ok", "matched": matched, "subscriptions": len(subs), "scanned": len(batch)}


def _matches(sub, ev, *, my_char_ids, attackers_by_km, count_by_km, staging) -> bool:
    et = sub.event_type
    if et == SubscriptionEventType.MY_LOSS:
        return bool(ev.victim_character_id) and ev.victim_character_id in my_char_ids
    if et == SubscriptionEventType.MY_LOSS_SRP_PENDING:
        return bool(ev.needs_srp and ev.victim_character_id and ev.victim_character_id in my_char_ids)
    if et == SubscriptionEventType.MY_KILL:
        return bool(my_char_ids & attackers_by_km.get(ev.killmail_id, frozenset()))
    if et == SubscriptionEventType.FILTER_MATCH:
        return _filter_matches(
            sub.params or {}, ev,
            attacker_count=count_by_km.get(ev.killmail_id, 0), staging=staging,
        )
    return False


def _attacker_maps(batch):
    """``(attacker_char_ids_by_km, attacker_count_by_km)`` for the batch — one query."""
    from .models import KillmailParticipant

    km_ids = [ev.killmail_id for ev in batch]
    chars: dict[int, set] = {}
    counts: dict[int, int] = {}
    rows = KillmailParticipant.objects.filter(
        killmail_id__in=km_ids, role=KillmailParticipant.Role.ATTACKER
    ).values_list("killmail_id", "character_id")
    for km_id, char_id in rows:
        counts[km_id] = counts.get(km_id, 0) + 1
        if char_id:
            chars.setdefault(km_id, set()).add(char_id)
    return ({k: frozenset(v) for k, v in chars.items()}, counts)


_STAGING_SENTINEL = object()


def _staging_for(subs):
    """Gate-hop distances from the active staging system, computed once if any filter needs it."""
    if not any(
        s.event_type == SubscriptionEventType.FILTER_MATCH
        and int((s.params or {}).get("max_jumps_from_staging", 0) or 0) > 0
        for s in subs
    ):
        return None
    probe = types.SimpleNamespace(max_jumps_from_staging=1)
    try:
        return killfeed_rules.staging_distances(probe)
    except Exception:  # noqa: BLE001 — staging is optional; a lookup failure just disables the clause
        return None


def _batch_names(batch) -> dict:
    entity_ids, type_ids, system_ids = set(), set(), set()
    for ev in batch:
        if ev.victim_character_id:
            entity_ids.add(ev.victim_character_id)
        if ev.victim_ship_type_id:
            type_ids.add(ev.victim_ship_type_id)
        if ev.system_id:
            system_ids.add(ev.system_id)
    return _resolve_names(entity_ids, type_ids, system_ids)


def _char_ids(user, cache: dict) -> frozenset:
    key = getattr(user, "id", None)
    if key in cache:
        return cache[key]
    ids = frozenset(user.characters.values_list("character_id", flat=True)) if user else frozenset()
    cache[key] = ids
    return ids


# --------------------------------------------------------------------------- #
#  Push hooks — rank_up / watchlist_hit (called from their existing emitters)
# --------------------------------------------------------------------------- #
def notify_user_event(*, event_type: str, user_id: int, title, summary="",
                      link: str = "", payload: dict | None = None) -> int:
    """Fan a per-user event (rank_up) out to that user's matching subscriptions. Returns count.

    Called from ``rank_notify`` after it has already sent the built-in celebration, so this
    adds only the member's chosen extra channels (email/webhook/rss and their in-app notify).
    ``title`` / ``summary`` may be ``gettext_lazy`` proxies — they are resolved under the
    recipient's own locale. Never raises into the caller.
    """
    if not _enabled():
        return 0
    try:
        subs = list(
            KillboardSubscription.objects.filter(
                enabled=True, event_type=event_type, user_id=user_id
            ).select_related("user")
        )
    except Exception:  # noqa: BLE001
        log.exception("kb subscription lookup failed for user %s / %s", user_id, event_type)
        return 0
    member_cache: dict = {}
    n = 0
    for sub in subs:
        if not _member_ok(sub.user, member_cache):
            continue
        with translation.override(_user_lang(sub.user)):
            rendered = {"title": str(title), "summary": str(summary), "link": link,
                        "payload": payload or {}, "killmail_id": None, "seq": None}
            if record_and_deliver(sub, rendered):
                n += 1
    return n


def notify_watchlist_hit(entry, ctx: dict, names: dict) -> int:
    """Fan a watchlist tripwire out to every member's matching ``watchlist_hit`` subscription.

    Called from ``watchlist_alerts._emit`` at the existing corp-broadcast point (the event is
    not re-derived). A subscription may pin a specific watchlist via ``params.watchlist_id``;
    otherwise it fires for any watched entity. Never raises into the caller.
    """
    if not _enabled():
        return 0
    kind = entry.get_entity_type_display().lower()
    ename = names.get(entry.entity_id) or f"{kind} #{entry.entity_id}"
    system = names.get(ctx.get("system_id")) or gettext("unknown space")
    payload = {
        "event": "watchlist_hit", "entity_type": kind, "entity_id": entry.entity_id,
        "entity_name": ename, "watchlist_id": entry.watchlist_id,
        "watchlist_name": entry.watchlist.name, "system_id": ctx.get("system_id"),
        "system_name": system, "killmail_id": ctx.get("km_id"),
    }
    try:
        subs = list(
            KillboardSubscription.objects.filter(
                enabled=True, event_type=SubscriptionEventType.WATCHLIST_HIT
            ).select_related("user")
        )
    except Exception:  # noqa: BLE001
        log.exception("kb watchlist_hit subscription lookup failed")
        return 0
    member_cache: dict = {}
    n = 0
    for sub in subs:
        pinned = (sub.params or {}).get("watchlist_id")
        if pinned and int(pinned) != entry.watchlist_id:
            continue
        if not _member_ok(sub.user, member_cache):
            continue
        with translation.override(_user_lang(sub.user)):
            title = gettext("Watchlist tripwire: %(entity)s") % {"entity": ename}
            summary = gettext("Watched %(kind)s %(entity)s seen in %(system)s (%(list)s).") % {
                "kind": kind, "entity": ename, "system": system, "list": entry.watchlist.name}
            rendered = {"title": title, "summary": summary, "link": _km_link(ctx.get("km_id")),
                        "payload": payload, "killmail_id": ctx.get("km_id"), "seq": None}
            if record_and_deliver(sub, rendered):
                n += 1
    return n


# --------------------------------------------------------------------------- #
#  Test-fire (the self-serve "send me a sample" button)
# --------------------------------------------------------------------------- #
def test_fire(sub: KillboardSubscription) -> tuple[bool, str]:
    """Send a representative sample event through the subscription's REAL channel.

    Returns ``(ok, message)``. The sample is recorded as a feed item too, so an RSS
    subscriber sees it in their feed. Delivery failures do NOT count toward the dead-letter
    ceiling (a test must never disable a live subscription)."""
    try:
        with translation.override(_user_lang(sub.user)):
            title = gettext("Test: %(kind)s") % {"kind": sub.get_event_type_display()}
            summary = gettext(
                "This is a sample %(kind)s notification from your killboard subscription."
            ) % {"kind": sub.get_event_type_display()}
            item = KillboardSubscriptionEvent.objects.create(
                subscription=sub, event_type=sub.event_type, title=title[:200],
                summary=summary[:500], payload={"event_type": sub.event_type, "test": True},
            )
            ok, err = _deliver(sub, item)
        _prune_feed(sub)
    except Exception as exc:  # noqa: BLE001
        log.exception("kb subscription %s test-fire failed", sub.id)
        return False, str(exc)
    if sub.channel == SubscriptionChannel.RSS:
        return True, gettext("A sample item was added to your RSS feed.")
    return ok, ("" if ok else err)
