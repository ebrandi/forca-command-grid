"""Killboard views: public corp killboard, plus intel watchlists and battle reports."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Page, Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from apps.sde.models import SdeSolarSystem
from apps.sde.search import search_systems
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import anatomy, fitrender
from .battle import generate_battle_report
from .forms import BattleReportForm, WatchlistEntryForm, WatchlistForm
from .intel import watchlist_overview
from .models import (
    BattleReport,
    Killmail,
    KillmailComment,
    KillmailParticipant,
    Watchlist,
    WatchlistEntry,
)

# Location/ship drill-down filters: keyed on the killmail's own columns, so they
# are side-independent. Value is (Killmail column, chip label, name kind).
_KM_FILTERS = {
    "system_id": ("solar_system_id", "System", "system"),
    "region_id": ("region_id", "Region", "region"),
    "ship_type_id": ("victim_ship_type_id", "Ship", "type"),
}
# Entity filters can match either the victim or any attacker, depending on the
# ``side`` toggle. Value is (victim column, participant column, label, name kind).
_ENTITY_FILTERS = {
    "character_id": ("victim_character_id", "character_id", "Pilot", "character"),
    "corporation_id": ("victim_corporation_id", "corporation_id", "Corp", "character"),
    "alliance_id": ("victim_alliance_id", "alliance_id", "Alliance", "character"),
}


# The public killfeed's unfiltered views (all / kills / losses × window × early
# pages) are the hot landing path; cache their evaluated rows briefly so each hit
# skips the per-request attacker-count aggregation + the unbounded pagination COUNT.
_LIST_CACHE_TTL = 60
_LIST_CACHE_VERSION = 1
_LIST_CACHE_MAX_PAGE = 50  # bound the keyspace; deep pages fall through to live
# The standard window options offered in the UI. Caching is limited to these so a
# raw ``?days=`` value can't mint an unbounded number of cache keys; any other
# (still-valid) window falls through to a live query.
_LIST_CACHE_DAYS = {"1", "7", "30"}


def _home() -> int:
    from django.conf import settings

    return getattr(settings, "FORCA_HOME_CORP_ID", 0)


class _CountPaginator(Paginator):
    """A Paginator whose total ``count`` is supplied (cached) rather than queried,
    so a cached page can be rebuilt without re-running the COUNT or the page query."""

    def __init__(self, count: int, per_page: int):
        super().__init__([], per_page)
        self._cached_count = count

    @property
    def count(self) -> int:
        return self._cached_count


def _page_from_cache(rows: list, count: int, number: int, per_page: int = 50) -> Page:
    return Page(rows, number, _CountPaginator(count, per_page))


def _filter_label(kind: str, value: int) -> str:
    """Human name for an active filter chip, resolved from SDE / EveName."""
    from apps.corporation.models import EveName

    if kind == "system":
        return SdeSolarSystem.objects.filter(system_id=value).values_list("name", flat=True).first() or str(value)
    if kind == "region":
        from apps.sde.models import SdeRegion
        return SdeRegion.objects.filter(region_id=value).values_list("name", flat=True).first() or str(value)
    if kind == "type":
        from apps.sde.models import SdeType
        return SdeType.objects.filter(type_id=value).values_list("name", flat=True).first() or str(value)
    return EveName.objects.filter(entity_id=value).values_list("name", flat=True).first() or str(value)


def _remove_url(request: HttpRequest, *params: str) -> str:
    """A "remove these filters" URL preserving the other params and resetting page
    (the querystring template tag can't take a dynamic key)."""
    remaining = request.GET.copy()
    for param in params:
        remaining.pop(param, None)
    remaining.pop("page", None)
    qstr = remaining.urlencode()
    return f"?{qstr}" if qstr else request.path


def _alliance_options() -> list[dict]:
    """Bounded, cached list of alliances seen on the home-corp killboard, to
    populate the alliance filter <select>. Top by involvement, resolved via EveName."""
    key = f"kb:alliance_opts:{_home()}"
    opts = cache.get(key)
    if opts is None:
        from django.db.models import Count

        from apps.corporation.models import EveName
        rows = (
            Killmail.objects.filter(involves_home_corp=True, victim_alliance_id__isnull=False)
            .values("victim_alliance_id")
            .annotate(n=Count("killmail_id"))
            .order_by("-n")[:40]
        )
        ids = [r["victim_alliance_id"] for r in rows]
        names = dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))
        opts = [{"id": i, "name": names.get(i) or str(i)} for i in ids]
        cache.set(key, opts, 300)
    return opts


def killboard_list(request: HttpRequest) -> HttpResponse:
    """Public corp killboard with click-to-drill-down filters and a time window."""
    qs = Killmail.objects.filter(involves_home_corp=True)

    kind = request.GET.get("kind")  # 'kills' | 'losses'
    if kind == "losses":
        qs = qs.filter(home_corp_role=Killmail.HomeRole.VICTIM)
    elif kind == "kills":
        qs = qs.filter(home_corp_role=Killmail.HomeRole.ATTACKER)

    active = []
    for param, (field, label, name_kind) in _KM_FILTERS.items():
        value = request.GET.get(param)
        if value and value.isdigit():
            qs = qs.filter(**{field: int(value)})
            active.append({
                "param": param, "label": label, "value": int(value),
                "name": _filter_label(name_kind, int(value)),
                "remove_url": _remove_url(request, param),
            })

    # Pilot/corp/alliance filters match the victim by default; the ``side`` toggle
    # flips them to any attacker. Attacker matches go through a killmail_id subquery
    # (not a join) so one killmail with many matching attackers isn't duplicated in
    # the feed and the fast indexed COUNT is preserved (see intel.py).
    side = request.GET.get("side")
    side = side if side in ("victim", "attacker") else "victim"
    entity_active = False
    for param, (vcol, pcol, label, name_kind) in _ENTITY_FILTERS.items():
        value = request.GET.get(param)
        if value and value.isdigit():
            ival = int(value)
            if side == "attacker":
                sub = KillmailParticipant.objects.filter(
                    role=KillmailParticipant.Role.ATTACKER, **{pcol: ival}
                ).values("killmail_id")
                qs = qs.filter(killmail_id__in=sub)
            else:
                qs = qs.filter(**{vcol: ival})
            entity_active = True
            active.append({
                "param": param,
                "label": f"{label} (atk)" if side == "attacker" else label,
                "value": ival,
                "name": _filter_label(name_kind, ival),
                "remove_url": _remove_url(request, param),
            })

    days = request.GET.get("days")
    if days and days.isdigit():
        from django.utils import timezone
        qs = qs.filter(killmail_time__gte=timezone.now() - timedelta(days=int(days)))

    from django.utils import timezone

    # Cache only the unfiltered (no drill-down) early pages — the common landing
    # path. Filtered drill-downs are rarer and per-combination, so they stay live
    # to keep the keyspace bounded.
    try:
        page_num = max(1, int(request.GET.get("page") or 1))
    except (TypeError, ValueError):
        page_num = 1
    # Build the key only from closed, normalised values — never raw query params —
    # so an attacker can't vary ?kind=/?days= to mint unbounded cache entries.
    # Filtered drill-downs stay uncached on purpose (per-combination = unbounded
    # keyspace); the crawler load they used to attract is now shed at the edge
    # (nginx blocks AI/SEO bots on faceted /killboard/?… URLs) rather than cached —
    # caching wouldn't help anyway, since each crawled filter URL is hit only once.
    kind_key = kind if kind in ("kills", "losses") else "all"
    days_cacheable = (not days) or (days in _LIST_CACHE_DAYS)
    cache_key = None
    if not active and days_cacheable and page_num <= _LIST_CACHE_MAX_PAGE:
        cache_key = f"kb:list:{_LIST_CACHE_VERSION}:{_home()}:{kind_key}:{days or 'all'}:{page_num}"
        cached = cache.get(cache_key)
        if cached is not None:
            rows, count, number = cached
            page = _page_from_cache(rows, count, number)
        else:
            page = None  # miss: build below, then store under cache_key
    else:
        page = None

    if page is None:
        # Paginate the PLAIN filtered queryset: its COUNT(*) is a fast indexed count,
        # not a COUNT over a GROUP BY across the 5.5M-row participant join (the
        # annotate() that previously preceded pagination made this count take ~3.5s on
        # 180k killmails, and far worse under load). Enrich only the page's 50 rows.
        from django.db.models import Count, Prefetch, Q

        paginator = Paginator(qs.order_by("-killmail_time"), 50)
        page = paginator.get_page(page_num)
        ids = [k.pk for k in page.object_list]
        enriched = {
            k.pk: k
            for k in Killmail.objects.filter(pk__in=ids)
            .annotate(attacker_count=Count("participants", filter=Q(participants__role="attacker")))
            .prefetch_related(Prefetch(
                "participants",
                queryset=KillmailParticipant.objects.filter(role="attacker", final_blow=True),
                to_attr="final_blowers",
            ))
        }
        page.object_list = [enriched[k.pk] for k in page.object_list if k.pk in enriched]
        if cache_key is not None:
            cache.set(cache_key, (list(page.object_list), paginator.count, page.number),
                      _LIST_CACHE_TTL)

    from .analytics import killfeed_overview

    now = timezone.now()
    # D5: htmx filter/page requests get just the feed fragment (same context).
    is_htmx = bool(request.headers.get("HX-Request"))
    template = "killboard/_feed.html" if is_htmx else "killboard/list.html"

    # KB-29 live feed: enhance the pristine (unfiltered) landing feed only — a drilled-down
    # view must not receive unfiltered live rows. The tip seq seeds the client cursor so it
    # streams only genuinely new kills. Only computed for the full-page landing render.
    from django.conf import settings as _settings

    stream_enabled = bool(getattr(_settings, "KILLBOARD_STREAM_ENABLED", True))
    live_enabled = stream_enabled and not active and not entity_active and not is_htmx
    stream_topics = "kills" if kind == "kills" else "losses" if kind == "losses" else "all"
    stream_tip = 0
    if live_enabled:
        from .stream import tip_seq

        stream_tip = tip_seq()
    return render(
        request,
        template,
        {
            "page": page, "kind": kind or "all", "active_filters": active, "days": days or "",
            "days_options": [("1", "24h"), ("7", "7d"), ("30", "30d")],
            "side": side, "entity_active": entity_active,
            "alliances": _alliance_options(), "alliance_id": request.GET.get("alliance_id") or "",
            # Any filtered/drill-down page (has a query string) is non-canonical, so
            # tell crawlers not to index it or follow its links into the filter space.
            "robots_noindex": bool(request.GET),
            "overview": killfeed_overview(),
            "today_str": now.strftime("%Y-%m-%d"),
            "yesterday_str": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
            "live_enabled": live_enabled,
            "stream_topics": stream_topics,
            "stream_tip": stream_tip,
        },
    )


def killmail_feed_row(request: HttpRequest, killmail_id: int) -> HttpResponse:
    """KB-29: render one killfeed row server-side for the live feed to prepend.

    Serves the same public-board row markup (``_feed_row.html``) the landing feed uses, enriched
    identically (attacker count + final blower). Home-corp mails only — the same public surface
    the board already exposes, so no extra gating is needed; a non-home / unknown id is a 404.
    """
    from django.db.models import Count, Prefetch, Q

    km = (
        Killmail.objects.filter(pk=killmail_id, involves_home_corp=True)
        .annotate(attacker_count=Count("participants", filter=Q(participants__role="attacker")))
        .prefetch_related(Prefetch(
            "participants",
            queryset=KillmailParticipant.objects.filter(role="attacker", final_blow=True),
            to_attr="final_blowers",
        ))
        .first()
    )
    if km is None:
        raise Http404("No such home-corp killmail.")
    return render(request, "killboard/_feed_row.html", {"km": km})


def killboard_rankings(request: HttpRequest) -> HttpResponse:
    """Public PvP rankings: leaderboards per time window, for prize challenges.

    Default (no ``?year=``) is unchanged — the live time-window boards. Adding
    ``?year=2026`` (optionally ``&month=7``) shows the historical rankings for that
    calendar period, read fast from the monthly aggregate.
    """
    from datetime import date

    from django.utils import formats, timezone
    from django.utils.dates import MONTHS

    from . import aggregation, ranks_i18n
    from .leaderboards import active_ladder, leaderboards, pilot_combat_card, window_choices

    def _period_label(y: int, m: int) -> str:
        # Django's month names, not calendar.month_abbr: the C library's names never
        # translate. Byte-identical to the old "%b %Y" output under English.
        return formats.date_format(date(y, m, 1), "M Y")

    now_year = timezone.now().year
    # EVE launched in 2003 — no killboard history predates it. Bounding the year
    # here is a hard requirement: an unbounded ?year= (e.g. 10000, or a huge digit
    # string) would otherwise reach _period_bounds → datetime(year, …), which
    # raises ValueError/OverflowError and 500s this public page.
    min_rank_year = 2003

    # KB-23: roll a person's alts up under their main across all the boards.
    by_main = request.GET.get("by") == "main"

    year_raw = (request.GET.get("year") or "").strip()
    month_raw = (request.GET.get("month") or "").strip()
    historical = year_raw.isdigit() and min_rank_year <= int(year_raw) <= now_year
    sel_year = sel_month = None
    prev_period = next_period = None

    if historical:
        sel_year = int(year_raw)
        if month_raw.isdigit() and 1 <= int(month_raw) <= 12:
            sel_month = int(month_raw)
        data = aggregation.historical_leaderboards(sel_year, sel_month, by_main=by_main)
        # Previous / next period for the nav — clamped to the valid range so a
        # boundary click can never produce an out-of-range year.
        if sel_month:
            pm, py = (12, sel_year - 1) if sel_month == 1 else (sel_month - 1, sel_year)
            nm, ny = (1, sel_year + 1) if sel_month == 12 else (sel_month + 1, sel_year)
            if py >= min_rank_year:
                prev_period = {"year": py, "month": pm, "label": _period_label(py, pm)}
            if ny <= now_year:
                next_period = {"year": ny, "month": nm, "label": _period_label(ny, nm)}
        else:
            if sel_year - 1 >= min_rank_year:
                prev_period = {"year": sel_year - 1, "month": "", "label": str(sel_year - 1)}
            if sel_year + 1 <= now_year:
                next_period = {"year": sel_year + 1, "month": "", "label": str(sel_year + 1)}
    else:
        window_key = request.GET.get("window", "month")
        data = leaderboards(window_key, by_main=by_main)

    # A logged-in member sees their own all-time standing up top — a personal hook.
    my_card = None
    if request.user.is_authenticated:
        main = pilots.acting_pilot(request.user)  # LP-3: my stats = the pilot I am flying
        if main:
            card = pilot_combat_card(main.character_id)
            if card.get("has_record"):
                my_card = {"name": main.name, **card}

    return render(
        request,
        "killboard/rankings.html",
        {
            **data,
            "windows": window_choices(),
            "my_card": my_card,
            # ``active_ladder`` caches raw English (its key is not language-scoped), so the
            # legend's seeded titles are translated here, per request, under the reader locale.
            "rank_ladder": [
                {**e, "name": ranks_i18n.rank_title_for(e["name"])} for e in active_ladder()
            ],
            "historical": historical,
            "by_main": by_main,
            "sel_year": sel_year,
            "sel_month": sel_month,
            "available_years": aggregation.available_years(),
            # django.utils.dates.MONTHS is a 1-indexed dict of translated month names —
            # calendar.month_name would emit English in every locale.
            "month_names": [(i, str(MONTHS[i])) for i in range(1, 13)],
            "prev_period": prev_period,
            "next_period": next_period,
        },
    )


def _can_view_stats(user) -> bool:
    """The combat stats dashboard is for corp members and registered alliance pilots."""
    from apps.corporation.access import is_service_alliance_pilot

    return rbac.has_role(user, rbac.ROLE_MEMBER) or is_service_alliance_pilot(user)


@login_required
def killboard_stats(request: HttpRequest) -> HttpResponse:
    """Corp combat analytics dashboard — charts for members + alliance pilots."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/stats.html", {"denied": True}, status=403)
    from .analytics import dashboard

    data = dashboard()
    ships = data["ships"]
    ship_boards = [
        {"title": gettext("Ships we destroy"), "color": "text-kill", "canvas": "kb-ships-killed",
         "kind": "kills", "rows": ships["killed"]},
        {"title": gettext("Ships we lose"), "color": "text-loss", "canvas": "kb-ships-lost",
         "kind": "losses", "rows": ships["lost"]},
    ]
    location_boards = [
        {"title": gettext("Top systems"), "param": "system_id",
         "rows": [{"id": s["system_id"], "name": s["name"], "count": s["count"]}
                  for s in data["systems"]]},
        {"title": gettext("Top regions"), "param": "region_id",
         "rows": [{"id": r["region_id"], "name": r["name"], "count": r["count"]}
                  for r in data["regions"]]},
    ]
    return render(request, "killboard/stats.html", {
        "denied": False,
        "summary": data["summary"],
        "monthly": data["monthly"],
        "ships": ships,
        "ship_classes": data["ship_classes"],
        "space": data["space"],
        "doctrine": data["doctrine"],
        "heatmap": data["heatmap"],
        "months_back": data["months_back"],
        "heatmap_days": data["heatmap_days"],
        "ship_boards": ship_boards,
        "location_boards": location_boards,
    })


@login_required
def killboard_roster(request: HttpRequest) -> HttpResponse:
    """Corp-wide combat roster — every pilot, ordered by name, each linking to
    their individual combat analytics. Members + registered alliance pilots."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/roster.html", {"denied": True}, status=403)
    from .leaderboards import corp_combat_roster

    roster = corp_combat_roster()
    return render(request, "killboard/roster.html", {
        "denied": False,
        "roster": roster,
        "pilot_count": len(roster),
    })


@login_required
def killboard_pilot(request: HttpRequest, character_id: int) -> HttpResponse:
    """Per-pilot combat analytics — members + alliance pilots only."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/pilot.html", {"denied": True}, status=403)
    from apps.corporation.models import EveName

    from .analytics import pilot_analytics
    from .milestones import milestones_for

    data = pilot_analytics(character_id)
    name = (
        EveName.objects.filter(entity_id=character_id).values_list("name", flat=True).first()
        or f"Pilot {character_id}"
    )
    ships = data["ships"]
    ship_boards = [
        {"title": gettext("Ships flown"), "color": "text-kill", "canvas": "pilot-ships-flown",
         "rows": ships["flown"]},
        {"title": gettext("Ships lost"), "color": "text-loss", "canvas": "pilot-ships-lost",
         "rows": ships["lost"]},
    ]
    return render(request, "killboard/pilot.html", {
        "denied": False,
        "pilot_name": name,
        "character_id": character_id,
        "card": data["card"],
        "monthly": data["monthly"],
        "ships": ships,
        "heatmap": data["heatmap"],
        "months_back": data["months_back"],
        "heatmap_days": data["heatmap_days"],
        "ship_boards": ship_boards,
        "systems": data["systems"],
        "milestones": milestones_for([character_id]),
    })


@login_required
def killboard_compare(request: HttpRequest) -> HttpResponse:
    """Overlay up to 5 pilots' monthly kill trends — members + alliance only."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/compare.html", {"denied": True}, status=403)
    from apps.corporation.models import EveName
    from apps.sso.models import EveCharacter

    from .analytics import compare_pilots

    ids = [int(x) for x in request.GET.get("pilots", "").split(",") if x.strip().isdigit()]
    add = request.GET.get("add", "")
    if add.isdigit() and int(add) not in ids:
        ids.append(int(add))
    remove = request.GET.get("remove", "")
    if remove.isdigit() and int(remove) in ids:
        ids.remove(int(remove))
    ids = list(dict.fromkeys(ids))[:5]

    data = compare_pilots(ids) if ids else {"labels": [], "series": [], "table": []}
    names = dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))
    for row in data["table"]:
        row["name"] = names.get(row["character_id"], f"Pilot {row['character_id']}")
    for s in data["series"]:
        s["name"] = names.get(s["character_id"], f"Pilot {s['character_id']}")

    members = list(
        EveCharacter.objects.filter(is_corp_member=True)
        .exclude(character_id__in=ids)
        .order_by("name")
        .values("character_id", "name")[:500]
    )
    return render(request, "killboard/compare.html", {
        "denied": False,
        "compare": data,
        "selected_ids": ids,
        "pilots_param": ",".join(str(i) for i in ids),
        "members": members,
    })


def killmail_detail(request: HttpRequest, killmail_id: int) -> HttpResponse:
    killmail = get_object_or_404(
        # Only ``items`` is iterated as a prefetch by the template; ``attackers`` below
        # is a role-filtered queryset (index-served by unique_together(killmail, role, seq)),
        # so a ``participants`` prefetch would be a wasted full load bypassed by .filter().
        Killmail.objects.select_related("doctrine_fit").prefetch_related("items"),
        killmail_id=killmail_id,
    )
    # Owner/officer are the only viewers allowed the sensitive panels below. Computed once
    # and reused by both the fit deviation and the SRP chip (SECURITY / PRD §B5).
    viewer_is_owner = bool(
        request.user.is_authenticated
        and killmail.victim_character_id
        and request.user.characters.filter(character_id=killmail.victim_character_id).exists()
    )
    can_see_private = viewer_is_owner or rbac.has_role(request.user, rbac.ROLE_OFFICER)

    # Fit deviation is sensitive (it implies a mistake): owner or officer only.
    deviation = getattr(killmail, "fit_deviation", None)
    if deviation is None or deviation.is_clean or not can_see_private:
        deviation = None

    # SRP status is sensitive too (payout ISK, denial reason): owner or officer only.
    srp = killmail.srp_claims.first() if can_see_private else None

    # KB-25: let the loss owner file an SRP claim straight from the detail page. Only when the
    # viewer owns the loss (same ownership seam apps/srp uses), no claim exists yet, and it's a
    # corp loss. The SRP eligibility/payout is computed by apps/srp — never duplicated here; the
    # POST goes to the existing srp:claim view. Ineligible losses surface the honest reason.
    srp_request = None
    if viewer_is_owner and srp is None and killmail.home_corp_role == Killmail.HomeRole.VICTIM:
        from apps.srp import services as srp_services

        info = srp_services.eligibility(killmail)
        srp_request = {
            "eligible": bool(info.get("eligible")),
            "payout": info.get("payout"),
            "payout_mode": info.get("payout_mode"),
            "loss_value": info.get("loss_value"),
            "doctrine": info.get("doctrine"),
            "explanation": info.get("explanation") or info.get("reason"),
        }

    attackers = list(killmail.participants.filter(role="attacker").order_by("-damage_done"))
    breakdown = anatomy.attacker_breakdown(
        killmail, attackers, _home(), anatomy.doctrine_hull_ids()
    )
    return render(
        request,
        "killboard/detail.html",
        {
            "killmail": killmail,
            "attackers": breakdown["rows"],
            "parties": breakdown["parties"],
            "deviation": deviation,
            # Radial fitting-window render (KB-21b). ``deviation`` is already gated to
            # owner/officer, so the off-doctrine overlay stays private to permitted viewers.
            "wheel": fitrender.build_fit_wheel(killmail, deviation),
            # KB-22 detail-anatomy polish.
            "srp": srp,
            "srp_request": srp_request,  # KB-25: owner-only "Request SRP" affordance.
            "value_tier": anatomy.value_tier(killmail.total_value),
            "related": anatomy.related_killmails(killmail),
            "battles": list(killmail.battle_reports.all()),
            "comments": list(killmail.comments.all()),
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def killmail_comment_create(request: HttpRequest, killmail_id: int) -> HttpResponse:
    """Post a member comment on a killmail (KB-22). Corp-private discussion."""
    killmail = get_object_or_404(Killmail, killmail_id=killmail_id)
    body = (request.POST.get("body") or "").strip()
    if not body:
        messages.error(request, gettext("A comment can’t be empty."))
        return redirect("killboard:detail", killmail_id=killmail_id)
    pilot = pilots.acting_pilot(request.user)
    comment = KillmailComment.objects.create(
        killmail=killmail,
        author=request.user,
        author_name=pilot.name if pilot else request.user.get_username(),
        author_character_id=pilot.character_id if pilot else None,
        body=body[:2000],
    )
    audit_log(
        request.user, "killmail_comment.create", target_type="killmail",
        target_id=str(killmail_id), metadata={"comment_id": comment.id}, ip=client_ip(request),
    )
    return redirect(f"{reverse('killboard:detail', args=[killmail_id])}#comments")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def killmail_comment_delete(request: HttpRequest, killmail_id: int, comment_id: int) -> HttpResponse:
    """Remove a comment — the author or an officer only (KB-22)."""
    comment = get_object_or_404(KillmailComment, id=comment_id, killmail_id=killmail_id)
    if comment.author_id == request.user.id or rbac.has_role(request.user, rbac.ROLE_OFFICER):
        comment.delete()
        audit_log(
            request.user, "killmail_comment.delete", target_type="killmail",
            target_id=str(killmail_id), metadata={"comment_id": comment_id}, ip=client_ip(request),
        )
    else:
        messages.error(request, gettext("You can’t remove that comment."))
    return redirect(f"{reverse('killboard:detail', args=[killmail_id])}#comments")


def killmail_eft(request: HttpRequest, killmail_id: int) -> HttpResponse:
    """The loss's fit as EFT text (copy straight into the game / Pyfa). Generated locally.

    Public like the detail page itself — the items are already shown there; this just
    reformats them. Gating still follows the killboard feature audience (middleware).
    """
    killmail = get_object_or_404(
        Killmail.objects.prefetch_related("items"), killmail_id=killmail_id
    )
    from apps.doctrines.killmail_import import eft_from_killmail
    return HttpResponse(eft_from_killmail(killmail), content_type="text/plain; charset=utf-8")


def killmail_fit_esi(request: HttpRequest, killmail_id: int) -> JsonResponse:
    """The loss's fit as an ESI-shaped fitting JSON. Generated locally, nothing leaves the box."""
    killmail = get_object_or_404(
        Killmail.objects.prefetch_related("items"), killmail_id=killmail_id
    )
    return JsonResponse(fitrender.esi_fitting(killmail))


@login_required
@role_required(rbac.ROLE_MEMBER)
def system_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the battle-report system picker."""
    return JsonResponse(search_systems(request.GET.get("q", ""), limit=20), safe=False)


# --- KB-28: personal API tokens (self-serve) ----------------------------------
_MAX_ACTIVE_API_TOKENS = 20


@login_required
@role_required(rbac.ROLE_MEMBER)
def api_tokens(request: HttpRequest) -> HttpResponse:
    """A member's personal killboard-API tokens: list, create, revoke.

    The freshly-minted plaintext is stashed in the session for exactly one render (popped
    here) so it is shown once and never re-displayed — only its hash is stored."""
    from django.conf import settings

    from .models import KillboardApiToken

    new_token = request.session.pop("kb_new_api_token", None)
    tokens = list(KillboardApiToken.objects.filter(user=request.user))
    return render(request, "killboard/api_tokens.html", {
        "tokens": tokens,
        "new_token": new_token,
        "public_read": getattr(settings, "KILLBOARD_API_PUBLIC_READ", False),
        "active_count": sum(1 for t in tokens if t.is_active),
        "max_tokens": _MAX_ACTIVE_API_TOKENS,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def api_token_create(request: HttpRequest) -> HttpResponse:
    from .models import KillboardApiToken

    active = KillboardApiToken.objects.filter(user=request.user, revoked_at__isnull=True).count()
    if active >= _MAX_ACTIVE_API_TOKENS:
        messages.error(request, gettext(
            "You already have the maximum number of active tokens. Revoke one first."
        ))
        return redirect("killboard:api_tokens")
    name = (request.POST.get("name") or "").strip()
    token, raw = KillboardApiToken.issue(request.user, name=name)
    # Stash the plaintext for the one-time reveal on the redirected GET, never in the DB.
    request.session["kb_new_api_token"] = raw
    audit_log(
        request.user, "killboard_api_token.create", target_type="killboard_api_token",
        target_id=str(token.id), metadata={"name": token.name}, ip=client_ip(request),
    )
    messages.success(request, gettext("API token created — copy it now, it won’t be shown again."))
    return redirect("killboard:api_tokens")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def api_token_revoke(request: HttpRequest, token_id: int) -> HttpResponse:
    from .models import KillboardApiToken

    token = get_object_or_404(KillboardApiToken, id=token_id, user=request.user)
    token.revoke()
    audit_log(
        request.user, "killboard_api_token.revoke", target_type="killboard_api_token",
        target_id=str(token.id), ip=client_ip(request),
    )
    messages.success(request, gettext("API token revoked."))
    return redirect("killboard:api_tokens")


# --- KB-30: per-pilot subscriptions -------------------------------------------
def _subscriptions_disabled_redirect(request):
    messages.error(request, gettext("Subscriptions are turned off on this instance."))
    return redirect("killboard:subscriptions")


def _parse_filter_clause(request: HttpRequest) -> dict:
    """A killfeed_rules-style clause dict from the saved-filter builder POST.

    Mirrors killfeed_config's parsing (same vocabularies), so the personal filter and the
    officer kill-feed speak the same clause language and reuse the same evaluator."""
    from decimal import Decimal, InvalidOperation

    from . import killfeed_rules

    def _dec(field):
        raw = (request.POST.get(field) or "").replace(",", "").strip()
        try:
            return str(max(Decimal("0"), Decimal(raw))) if raw else "0"
        except InvalidOperation:
            return "0"

    def _int(field):
        raw = (request.POST.get(field) or "").strip()
        return int(raw) if raw.isdigit() else 0

    valid_bands = {v for v, _label in killfeed_rules.SEC_BANDS}
    valid_classes = set(killfeed_rules.SHIP_CLASSES)
    return {
        "min_loss_value": _dec("min_loss_value"),
        "min_kill_value": _dec("min_kill_value"),
        "exclude_npc": request.POST.get("exclude_npc") == "1",
        "exclude_awox": request.POST.get("exclude_awox") == "1",
        "require_solo": request.POST.get("require_solo") == "1",
        "min_attackers": _int("min_attackers"),
        "max_attackers": _int("max_attackers"),
        "sec_bands": [b for b in request.POST.getlist("sec_bands") if b in valid_bands],
        "ship_classes": [c for c in request.POST.getlist("ship_classes") if c in valid_classes],
        "max_jumps_from_staging": _int("max_jumps_from_staging"),
        "losses_deviated_only": request.POST.get("losses_deviated_only") == "1",
    }


@login_required
@role_required(rbac.ROLE_MEMBER)
def subscriptions(request: HttpRequest) -> HttpResponse:
    """A member's self-serve killboard subscriptions: list, create, toggle, delete, test."""
    from django.conf import settings

    from . import killfeed_rules
    from . import subscriptions as subs
    from .models import KillboardSubscription, SubscriptionChannel, SubscriptionEventType

    enabled = getattr(settings, "KILLBOARD_SUBSCRIPTIONS_ENABLED", True)
    rows = list(
        KillboardSubscription.objects.filter(user=request.user).prefetch_related("feed_events")
    )
    return render(request, "killboard/subscriptions.html", {
        "subscriptions": rows,
        "feature_enabled": enabled,
        "event_types": SubscriptionEventType.choices,
        "channels": SubscriptionChannel.choices,
        "sec_bands": killfeed_rules.SEC_BANDS,
        "ship_classes": killfeed_rules.SHIP_CLASSES,
        "watchlists": list(Watchlist.objects.all().values("id", "name")),
        "max_subscriptions": subs.per_user_cap(),
        "active_count": len(rows),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def subscription_create(request: HttpRequest) -> HttpResponse:
    from django.conf import settings

    from . import subscriptions as subs
    from .models import KillboardSubscription, SubscriptionChannel, SubscriptionEventType

    if not getattr(settings, "KILLBOARD_SUBSCRIPTIONS_ENABLED", True):
        return _subscriptions_disabled_redirect(request)

    if KillboardSubscription.objects.filter(user=request.user).count() >= subs.per_user_cap():
        messages.error(request, gettext(
            "You already have the maximum number of subscriptions. Delete one first."
        ))
        return redirect("killboard:subscriptions")

    event_type = request.POST.get("event_type") or ""
    channel = request.POST.get("channel") or ""
    if event_type not in SubscriptionEventType.values or channel not in SubscriptionChannel.values:
        messages.error(request, gettext("Pick a valid event and channel."))
        return redirect("killboard:subscriptions")

    sub = KillboardSubscription(user=request.user, event_type=event_type, channel=channel)

    # Per-channel fields.
    if channel == SubscriptionChannel.WEBHOOK:
        url = (request.POST.get("webhook_url") or "").strip()
        err = subs.webhook_url_error(url)
        if err:
            messages.error(request, err)
            return redirect("killboard:subscriptions")
        sub.webhook_url = url
    elif channel == SubscriptionChannel.EMAIL and not (request.user.email or "").strip():
        messages.error(request, gettext(
            "Add an email address to your account before subscribing by email."
        ))
        return redirect("killboard:subscriptions")
    elif channel == SubscriptionChannel.RSS:
        sub.regenerate_rss_token()

    # Per-event params.
    if event_type == SubscriptionEventType.FILTER_MATCH:
        clause = _parse_filter_clause(request)
        if not subs.clause_is_meaningful(clause):
            messages.error(request, gettext(
                "Set a minimum kill or loss value above 0 — a filter with both at 0 matches nothing."
            ))
            return redirect("killboard:subscriptions")
        sub.params = clause
    elif event_type == SubscriptionEventType.WATCHLIST_HIT:
        raw = (request.POST.get("watchlist_id") or "").strip()
        if raw.isdigit() and Watchlist.objects.filter(id=int(raw)).exists():
            sub.params = {"watchlist_id": int(raw)}

    # Start the cursor at the current tip so a new subscription never back-fires history.
    if sub.is_killmail_event:
        from .stream import tip_seq

        sub.last_seq = tip_seq()
    sub.save()
    audit_log(request.user, "killboard_subscription.create", target_type="killboard_subscription",
              target_id=str(sub.id), metadata={"event_type": event_type, "channel": channel},
              ip=client_ip(request))
    messages.success(request, gettext("Subscription created."))
    return redirect("killboard:subscriptions")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def subscription_toggle(request: HttpRequest, sub_id: int) -> HttpResponse:
    from .models import KillboardSubscription

    sub = get_object_or_404(KillboardSubscription, id=sub_id, user=request.user)
    sub.enabled = not sub.enabled
    fields = ["enabled", "updated_at"]
    if sub.enabled:
        # Re-enabling clears the dead-letter state and resumes from the current tip (skips the
        # backlog that accrued while it was disabled).
        sub.consecutive_failures = 0
        sub.disabled_reason = ""
        fields += ["consecutive_failures", "disabled_reason"]
        if sub.is_killmail_event:
            from .stream import tip_seq

            sub.last_seq = tip_seq()
            fields.append("last_seq")
    sub.save(update_fields=fields)
    audit_log(request.user, "killboard_subscription.toggle", target_type="killboard_subscription",
              target_id=str(sub.id), metadata={"enabled": sub.enabled}, ip=client_ip(request))
    messages.success(request, gettext("Subscription updated."))
    return redirect("killboard:subscriptions")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def subscription_delete(request: HttpRequest, sub_id: int) -> HttpResponse:
    from .models import KillboardSubscription

    sub = get_object_or_404(KillboardSubscription, id=sub_id, user=request.user)
    sub.delete()
    audit_log(request.user, "killboard_subscription.delete", target_type="killboard_subscription",
              target_id=str(sub_id), ip=client_ip(request))
    messages.success(request, gettext("Subscription deleted."))
    return redirect("killboard:subscriptions")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def subscription_test(request: HttpRequest, sub_id: int) -> HttpResponse:
    from . import subscriptions as subs
    from .models import KillboardSubscription

    sub = get_object_or_404(KillboardSubscription, id=sub_id, user=request.user)
    ok, detail = subs.test_fire(sub)
    if ok:
        messages.success(request, detail or gettext("Test notification sent."))
    else:
        messages.error(request, gettext("Test delivery failed: %(detail)s") % {"detail": detail})
    return redirect("killboard:subscriptions")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def subscription_rss_regenerate(request: HttpRequest, sub_id: int) -> HttpResponse:
    from .models import KillboardSubscription, SubscriptionChannel

    sub = get_object_or_404(
        KillboardSubscription, id=sub_id, user=request.user, channel=SubscriptionChannel.RSS
    )
    sub.regenerate_rss_token()
    sub.save(update_fields=["rss_token", "updated_at"])
    audit_log(request.user, "killboard_subscription.rss_regenerate",
              target_type="killboard_subscription", target_id=str(sub.id), ip=client_ip(request))
    messages.success(request, gettext("A new feed URL was generated — the old one no longer works."))
    return redirect("killboard:subscriptions")


def subscription_feed(request: HttpRequest, rss_token: str) -> HttpResponse:
    """The tokenised, session-less RSS/Atom feed for one subscription (KB-30).

    Token-authenticated only: no session, no CSRF, no account credential. Serves only the
    member-visible matched events already recorded for the subscription — nothing beyond what
    the owning member sees on the board. Revoked by regenerating the token or deleting the row.
    """
    from django.conf import settings
    from django.utils.feedgenerator import Atom1Feed

    from .models import KillboardSubscription, SubscriptionChannel

    if not getattr(settings, "KILLBOARD_SUBSCRIPTIONS_ENABLED", True):
        raise Http404("Subscriptions are disabled.")
    sub = (
        KillboardSubscription.objects.select_related("user")
        .filter(rss_token=rss_token, channel=SubscriptionChannel.RSS).first()
    )
    # A blank/absent token must never match a NULL column; require a real token and a still-member owner.
    if sub is None or not rss_token or not rbac.has_role(sub.user, rbac.ROLE_MEMBER):
        raise Http404("No such feed.")

    feed_url = request.build_absolute_uri()
    feed = Atom1Feed(
        title=gettext("Killboard: %(kind)s") % {"kind": sub.get_event_type_display()},
        link=request.build_absolute_uri("/killboard/"),
        description=gettext("Your personal killboard subscription feed."),
        language=getattr(sub.user, "language", "") or "en",
        feed_url=feed_url,
    )
    for item in sub.feed_events.all()[:50]:
        link = request.build_absolute_uri(item.link) if item.link else feed_url
        feed.add_item(
            title=item.title,
            link=link,
            description=item.summary or item.title,
            unique_id=f"kbsub:{sub.id}:{item.id}",
            updateddate=item.created,
            pubdate=item.created,
        )
    return HttpResponse(feed.writeString("utf-8"), content_type="application/atom+xml; charset=utf-8")


# --- Intel: watchlists --------------------------------------------------------
@login_required
@role_required(rbac.ROLE_MEMBER)
def watchlists(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "killboard/watchlists.html",
        {
            "watchlists": Watchlist.objects.prefetch_related("entries").order_by("name"),
            "reports": BattleReport.objects.order_by("-created_at")[:10],
            "can_manage": rbac.has_role(request.user, rbac.ROLE_OFFICER),
            "form": WatchlistForm(),
            "battle_form": BattleReportForm(initial={"hours": 24}),
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def watchlist_detail(request: HttpRequest, pk: int) -> HttpResponse:
    watchlist = get_object_or_404(Watchlist, pk=pk)
    return render(
        request,
        "killboard/watchlist_detail.html",
        {
            "watchlist": watchlist,
            "overview": watchlist_overview(watchlist, per_entry=5),
            "can_manage": rbac.has_role(request.user, rbac.ROLE_OFFICER),
            "entry_form": WatchlistEntryForm(),
        },
    )


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def watchlist_create(request: HttpRequest) -> HttpResponse:
    form = WatchlistForm(request.POST)
    if form.is_valid():
        wl = form.save()
        messages.success(request, gettext("Watchlist “%(name)s” created.") % {"name": wl.name})
        return redirect("killboard:watchlist_detail", pk=wl.pk)
    messages.error(request, gettext("Could not create watchlist."))
    return redirect("killboard:watchlists")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def watchlist_add_entry(request: HttpRequest, pk: int) -> HttpResponse:
    watchlist = get_object_or_404(Watchlist, pk=pk)
    form = WatchlistEntryForm(request.POST)
    if form.is_valid():
        entry = form.save(commit=False)
        entry.watchlist = watchlist
        try:
            entry.save()
            messages.success(request, gettext("Target added to watchlist."))
        except Exception:  # noqa: BLE001 - unique_together clash = already watched
            messages.error(request, gettext("That entity is already on this watchlist."))
    else:
        messages.error(request, gettext("Could not add target — check the entity id."))
    return redirect("killboard:watchlist_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def watchlist_remove_entry(request: HttpRequest, pk: int, entry_id: int) -> HttpResponse:
    get_object_or_404(WatchlistEntry, pk=entry_id, watchlist_id=pk).delete()
    messages.success(request, gettext("Target removed."))
    return redirect("killboard:watchlist_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def watchlist_delete(request: HttpRequest, pk: int) -> HttpResponse:
    get_object_or_404(Watchlist, pk=pk).delete()
    messages.success(request, gettext("Watchlist deleted."))
    return redirect("killboard:watchlists")


# --- Intel: battle reports ----------------------------------------------------
# Swing-sparkline canvas dimensions (server-rendered SVG); shared by view + template.
_SWING_W, _SWING_H = 240, 48


def _battle_report_context(request: HttpRequest, report: BattleReport, *, public: bool) -> dict:
    """Shared render context for the member (pk) and public (slug) battle pages.

    Officer-only overlays (SRP liability, doctrine compliance) are computed only
    when the viewer is an officer, so an anonymous/public viewer never sees them.
    """
    from apps.corporation.models import EveName
    from apps.sde.models import SdeType

    from . import battle_sides

    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)

    # v1 per-corp charts (kept — the ISK-by-side bar + ships doughnut).
    corps = report.sides.get("corporations", [])[:8]
    corp_names = dict(
        EveName.objects.filter(entity_id__in=[c["corporation_id"] for c in corps])
        .values_list("entity_id", "name")
    )
    sides_chart = {
        "labels": [corp_names.get(c["corporation_id"], str(c["corporation_id"])) for c in corps],
        "isk_lost": [float(c["isk_lost"]) for c in corps],
        "kills": [c["kills"] for c in corps],
        "losses": [c["losses"] for c in corps],
    }
    ship_items = list(report.ship_breakdown.items())[:12]  # stored sorted desc
    ship_names = dict(
        SdeType.objects.filter(type_id__in=[int(t) for t, _ in ship_items])
        .values_list("type_id", "name")
    )
    ships_chart = {
        "labels": [ship_names.get(int(t), f"Type {t}") for t, _ in ship_items],
        "counts": [n for _, n in ship_items],
    }

    # v2 co-occurrence sides.
    sides = list(report.detected_sides.prefetch_related("members"))
    entity_ids = [m.entity_id for s in sides for m in s.members.all()]
    entity_names = dict(
        EveName.objects.filter(entity_id__in=entity_ids).values_list("entity_id", "name")
    )
    home_side = next((s for s in sides if s.is_home_side), None)
    reference = home_side or (sides[0] if sides else None)

    side_views = []
    move_targets = [(s.index, s.label) for s in sides]
    for s in sides:
        members = [
            {
                "entity_type": m.entity_type, "entity_id": m.entity_id,
                "name": entity_names.get(m.entity_id) or f"{m.entity_type} {m.entity_id}",
                "kills": m.kills, "losses": m.losses, "isk_lost": m.isk_lost,
                "is_manual": m.is_manual,
            }
            for m in s.members.all()
        ]
        srp = battle_sides.srp_liability(report, s) if (is_officer and s.is_home_side) else None
        compliance = battle_sides.doctrine_compliance(report, s) if is_officer else None
        side_views.append({
            "index": s.index, "label": s.label, "is_home_side": s.is_home_side,
            "kills": s.kills, "losses": s.losses,
            "isk_destroyed": s.isk_destroyed, "isk_lost": s.isk_lost,
            "pilot_count": s.pilot_count, "efficiency": round(s.efficiency * 100),
            "members": members, "srp": srp, "compliance": compliance,
        })

    timeline = battle_sides.battle_timeline(report, reference)
    # Readiness context: member-visible (ops are member data), so shown on both pages.
    op = battle_sides.op_overlap(report)

    return {
        "report": report,
        "public": public,
        "sides_chart": sides_chart,
        "ships_chart": ships_chart,
        "side_views": side_views,
        "move_targets": move_targets,
        "can_reassign": is_officer and not public,
        "timeline": timeline,
        "swing_series": [float(r["swing"]) for r in timeline["rows"]],
        "swing_w": _SWING_W, "swing_h": _SWING_H,
        "swing_baseline_y": battle_sides.swing_baseline_y(_SWING_H),
        "readiness_op": op,
        "permalink": request.build_absolute_uri(
            reverse("killboard:battle_report_public", args=[report.slug])
        ),
        "brevetools_url": battle_sides.brevetools_url(report),
    }


@login_required
@role_required(rbac.ROLE_MEMBER)
def battle_report_detail(request: HttpRequest, pk: int) -> HttpResponse:
    report = get_object_or_404(BattleReport, pk=pk)
    return render(request, "killboard/battle_report.html",
                  _battle_report_context(request, report, public=False))


def battle_report_public(request: HttpRequest, slug: str) -> HttpResponse:
    """Anonymous-viewable permalink for a PUBLIC report.

    Mirrors the public killmail pages (no login). A private report 404s here so its
    existence isn't leaked — members reach it at the member-gated pk URL instead.
    Officer overlays stay suppressed for anyone below officer (an anon viewer always
    is), so the shareable page carries no sensitive SRP/deviation figures.
    """
    report = get_object_or_404(BattleReport, slug=slug, is_public=True)
    return render(request, "killboard/battle_report.html",
                  _battle_report_context(request, report, public=True))


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def battle_report_side_move(request: HttpRequest, pk: int) -> HttpResponse:
    """Officer: move an entity (corp/pilot) to another detected side (KB-31)."""
    from . import battle_sides

    report = get_object_or_404(BattleReport, pk=pk)
    entity_type = request.POST.get("entity_type", "")
    try:
        entity_id = int(request.POST.get("entity_id", ""))
        side_index = int(request.POST.get("side_index", ""))
    except (TypeError, ValueError):
        messages.error(request, gettext("Invalid reassignment."))
        return redirect("killboard:battle_report_detail", pk=pk)
    if battle_sides.move_entity(report, entity_type, entity_id, side_index, actor=request.user):
        audit_log(request.user, "battle_report.side_move", target_type="battle_report",
                  target_id=str(report.pk),
                  metadata={"entity_type": entity_type, "entity_id": entity_id,
                            "side_index": side_index}, ip=client_ip(request))
        messages.success(request, gettext("Side reassigned."))
    else:
        messages.error(request, gettext("Couldn't reassign to that side."))
    return redirect("killboard:battle_report_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def battle_report_recompute(request: HttpRequest, pk: int) -> HttpResponse:
    """Officer: re-run side detection (preserving manual overrides) (KB-31)."""
    from . import battle_sides

    report = get_object_or_404(BattleReport, pk=pk)
    battle_sides.recompute_sides(report)
    audit_log(request.user, "battle_report.recompute", target_type="battle_report",
              target_id=str(report.pk), ip=client_ip(request))
    messages.success(request, gettext("Sides recomputed."))
    return redirect("killboard:battle_report_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def battle_report_create(request: HttpRequest) -> HttpResponse:
    form = BattleReportForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Pick a system and a window (1–168h)."))
        return redirect("killboard:watchlists")
    system_id = form.cleaned_data["system_id"]
    if not SdeSolarSystem.objects.filter(system_id=system_id).exists():
        messages.error(request, gettext("Pick a system from the list."))
        return redirect("killboard:watchlists")
    report = generate_battle_report(
        system_id, hours=form.cleaned_data["hours"], title=form.cleaned_data.get("title", "")
    )
    if not report:
        messages.error(request, gettext("No killmails in that system/window to report on."))
        return redirect("killboard:watchlists")
    audit_log(request.user, "battle_report.create", target_type="battle_report",
              target_id=str(report.id), metadata={"system_id": system_id}, ip=client_ip(request))
    messages.success(request, gettext("Battle report generated."))
    return redirect("killboard:battle_report_detail", pk=report.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
def killfeed_config(request: HttpRequest) -> HttpResponse:
    """Officer settings: the Discord kill feed + the optional realtime ingest fallback (KB-20)."""
    from decimal import Decimal, InvalidOperation

    from .ingest_health import ingest_status
    from .models import KillFeedConfig, KillstreamState

    cfg = KillFeedConfig.load()
    ks = KillstreamState.load()
    if request.method == "POST":
        # The realtime-fallback toggle is a separate form on the same page; a run in
        # flight never writes ``enabled`` (see killstream._STATE_FIELDS), so this is the
        # only writer of it.
        if request.POST.get("section") == "killstream":
            ks.enabled = request.POST.get("killstream_enabled") == "1"
            ks.save(update_fields=["enabled", "updated_at"])
            messages.success(request, gettext("Real-time fallback setting saved."))
            return redirect("killboard:killfeed_config")

        cfg.enabled = request.POST.get("enabled") == "1"

        def _dec(field, current):
            raw = (request.POST.get(field) or "").replace(",", "").strip()
            try:
                return max(Decimal("0"), Decimal(raw)) if raw else Decimal("0")
            except InvalidOperation:
                return current

        def _int(field):
            raw = (request.POST.get(field) or "").strip()
            return int(raw) if raw.isdigit() else 0

        cfg.min_loss_value = _dec("min_loss_value", cfg.min_loss_value)
        cfg.min_kill_value = _dec("min_kill_value", cfg.min_kill_value)

        # KB-24 rule clauses — validate the multi-selects against the known vocabularies.
        from . import killfeed_rules
        valid_bands = {v for v, _label in killfeed_rules.SEC_BANDS}
        valid_classes = set(killfeed_rules.SHIP_CLASSES)
        cfg.exclude_npc = request.POST.get("exclude_npc") == "1"
        cfg.exclude_awox = request.POST.get("exclude_awox") == "1"
        cfg.require_solo = request.POST.get("require_solo") == "1"
        cfg.min_attackers = _int("min_attackers")
        cfg.max_attackers = _int("max_attackers")
        cfg.sec_bands = [b for b in request.POST.getlist("sec_bands") if b in valid_bands]
        cfg.ship_classes = [c for c in request.POST.getlist("ship_classes") if c in valid_classes]
        cfg.max_jumps_from_staging = _int("max_jumps_from_staging")
        cfg.losses_deviated_only = request.POST.get("losses_deviated_only") == "1"
        cfg.save(update_fields=[
            "enabled", "min_loss_value", "min_kill_value",
            "exclude_npc", "exclude_awox", "require_solo", "min_attackers", "max_attackers",
            "sec_bands", "ship_classes", "max_jumps_from_staging", "losses_deviated_only",
            "updated_at",
        ])
        messages.success(request, gettext("Kill-feed settings saved."))
        return redirect("killboard:killfeed_config")
    from . import killfeed_rules
    return render(
        request,
        "killboard/killfeed_config.html",
        {
            "cfg": cfg, "ks": ks, "ingest": ingest_status(),
            "sec_bands": killfeed_rules.SEC_BANDS,
            "ship_classes": killfeed_rules.SHIP_CLASSES,
        },
    )
