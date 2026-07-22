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
    from . import branding as _branding
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
            # KB-38 branding overlay (WS-D5): optional corp name override + accent applied to
            # the hero. Cached AppSetting read; empty values fall back to today's look.
            "branding": _branding.get_branding(),
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
def killboard_meta(request: HttpRequest) -> HttpResponse:
    """KB-36 meta boards (WS-D2): matchup ("what kills X"), hull performance, weapon board.

    Member-gated (the analytics audience: corp members + registered alliance pilots). Every
    board is mined from OUR OWN killmail history — never universal — which the page states.
    """
    if not _can_view_stats(request.user):
        return render(request, "killboard/meta.html", {"denied": True}, status=403)
    from . import meta

    window = meta.resolve_window((request.GET.get("window") or "").strip())
    raw_hull = (request.GET.get("hull") or "").strip()
    hull_id = int(raw_hull) if raw_hull.isdigit() else None
    data = meta.meta_page(window, hull_id)
    return render(request, "killboard/meta.html", {
        "denied": False,
        "home_corp_id": _home(),
        # Members viewing intel: hull rows link to adversary/pilot pages where relevant.
        "intel_links": True,
        **data,
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
def killboard_pilot_cv(request: HttpRequest, character_id: int) -> HttpResponse:
    """KB-37 PVP CV — a pilot's whole combat identity: card, ranks, trophies, milestones,
    Kill-of-the-Week mentions, season placements and signature stats. Members + alliance pilots."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/cv.html", {"denied": True}, status=403)
    from apps.corporation.models import EveName

    from .cv import pilot_cv

    name = (
        EveName.objects.filter(entity_id=character_id).values_list("name", flat=True).first()
        or f"Pilot {character_id}"
    )
    return render(request, "killboard/cv.html", {
        "denied": False,
        "pilot_name": name,
        **pilot_cv(character_id),
    })


@login_required
def killboard_trophies(request: HttpRequest) -> HttpResponse:
    """KB-37 trophy hall — the corp trophy catalogue, the Trophy Leaderboard, and (for a logged-in
    member) their own earned trophies. Members + registered alliance pilots."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/trophies.html", {"denied": True}, status=403)
    from . import trophies as trophy_svc
    from .models import TrophyCategory, TrophyDefinition

    catalogue = list(TrophyDefinition.objects.filter(enabled=True))
    my_trophies = []
    my_progress = []
    main = pilots.acting_pilot(request.user)
    if main:
        my_trophies = trophy_svc.pilot_trophies(main.character_id)
        my_progress = trophy_svc.trophy_progress_toward_next(main.character_id)
    return render(request, "killboard/trophies.html", {
        "denied": False,
        "catalogue": catalogue,
        "categories": TrophyCategory.choices,
        "leaderboard": trophy_svc.trophy_leaderboard(),
        "my_trophies": my_trophies,
        "my_progress": my_progress,
        "my_character_id": main.character_id if main else None,
    })


@login_required
def killboard_seasons(request: HttpRequest) -> HttpResponse:
    """KB-37 seasonal ladders — the quarterly podium boards. Default is the current quarter;
    ``?year=&q=`` opens a past season. Members + registered alliance pilots."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/seasons.html", {"denied": True}, status=403)
    from . import seasons

    cy, cq = seasons.current_quarter()
    year_raw = (request.GET.get("year") or "").strip()
    q_raw = (request.GET.get("q") or "").strip()
    year = int(year_raw) if year_raw.isdigit() else cy
    quarter = int(q_raw) if (q_raw.isdigit() and 1 <= int(q_raw) <= 4) else cq
    payload = seasons.season_payload(year, quarter)
    labeled_boards = [
        {"key": k, "label": seasons.board_label(k), "rows": payload["boards"].get(k, [])}
        for k in seasons.BOARD_KEYS
    ]
    return render(request, "killboard/seasons.html", {
        "denied": False,
        "season": payload,
        "boards": labeled_boards,
        "available": seasons.available_seasons(),
    })


@login_required
def killboard_kotw(request: HttpRequest) -> HttpResponse:
    """KB-37 Kill-of-the-Week hall — the recent weekly standouts. Members + alliance pilots."""
    if not _can_view_stats(request.user):
        return render(request, "killboard/kotw.html", {"denied": True}, status=403)
    from . import kotw

    return render(request, "killboard/kotw.html", {
        "denied": False,
        "entries": kotw.recent_kotw(),
        "can_override": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def killboard_kotw_override(request: HttpRequest) -> HttpResponse:
    """Officer override: pin a specific home kill as a week's Kill of the Week (audited)."""
    from . import kotw

    km_raw = (request.POST.get("killmail_id") or "").strip()
    year_raw = (request.POST.get("iso_year") or "").strip()
    week_raw = (request.POST.get("iso_week") or "").strip()
    if not (km_raw.isdigit() and year_raw.isdigit() and week_raw.isdigit()):
        messages.error(request, gettext("Provide a killmail id, ISO year and ISO week."))
        return redirect("killboard:kotw")
    km = Killmail.objects.filter(
        killmail_id=int(km_raw), involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.ATTACKER,
    ).first()
    if km is None:
        messages.error(request, gettext("That killmail isn't a home-corp kill."))
        return redirect("killboard:kotw")
    row = kotw.set_override(int(year_raw), int(week_raw), km, request.user)
    audit_log(
        request.user, "killboard.kotw_override",
        target_type="killboard_kotw", target_id=f"{row.iso_year}:{row.iso_week}",
        metadata={"killmail_id": km.killmail_id}, ip=client_ip(request),
    )
    messages.success(request, gettext("Kill of the Week updated for %(year)s-W%(week)02d.") % {
        "year": row.iso_year, "week": row.iso_week})
    return redirect("killboard:kotw")


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

    # KB-35: "value then vs now". ``value_at_kill`` (stored) is the price on the day it died;
    # "now" is recomputed lazily from current prices so it's live regardless of the last
    # re-value beat — no second figure is stored. For owner/officer (the SRP-dispute audience)
    # we also resolve each item's at-kill price source for the auditable breakdown, reading
    # local history only (never a network fetch on a page view).
    from apps.killboard.valuation import compute_value
    from apps.market.pricing import build_price_index

    wheel = fitrender.build_fit_wheel(killmail, deviation)
    now_total = compute_value(killmail, build_price_index(), persist_items=False)["total_value"]
    then_total = (
        killmail.value_at_kill if killmail.value_at_kill is not None else killmail.total_value
    )
    _source_labels = {
        "everef_history": gettext("priced at kill date (EVE Ref market history)"),
        "live": gettext("priced live (fresh kill)"),
        "live_fallback": gettext("priced live (no history for the kill date)"),
        "fuzzwork_pct": gettext("Fuzzwork percentile (high-value)"),
        "janice": gettext("Janice (PLEX / injectors)"),
        "mixed": gettext("mixed price sources"),
        "unpriced": gettext("no market price"),
    }
    valuation = {
        "then": then_total,
        "now": now_total,
        "delta": now_total - then_total,
        "source": killmail.value_source or "",
        "source_label": _source_labels.get(killmail.value_source, killmail.value_source or ""),
        "has_at_kill": killmail.value_at_kill is not None,
    }
    if can_see_private:
        from apps.market.historical import HistoricalPriceLookup

        lookup = HistoricalPriceLookup(killmail.killmail_time, fetch=False)
        lookup(killmail.victim_ship_type_id)
        wheel["hull_source"] = lookup.type_sources.get(killmail.victim_ship_type_id, "")
        for group in wheel["table"]:
            for item in group["items"]:
                lookup(item["type_id"])
                item["source"] = lookup.type_sources.get(item["type_id"], "")

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
            "wheel": wheel,
            "valuation": valuation,  # KB-35 then-vs-now
            # KB-22 detail-anatomy polish.
            "srp": srp,
            "srp_request": srp_request,  # KB-25: owner-only "Request SRP" affordance.
            "value_tier": anatomy.value_tier(killmail.total_value),
            "related": anatomy.related_killmails(killmail),
            "battles": list(killmail.battle_reports.all()),
            "comments": list(killmail.comments.all()),
            # KB-33: entity names link to adversary pages (non-home) or the pilot page (home).
            # Only for members — the public detail page keeps plain names for anonymous viewers.
            "intel_links": rbac.has_role(request.user, rbac.ROLE_MEMBER),
            "home_corp_id": _home(),
            "victim_is_home": bool(
                killmail.victim_corporation_id
                and killmail.victim_corporation_id == _home()
            ),
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


# --- KB-33 adversary entity pages (WS-C3) -------------------------------------
# Member-gated intel: a profile of a NON-home character/corp/alliance built ONLY from our own
# killmail history (apps/killboard/adversary.py) — never universal, every figure relative to
# us. An entity with zero history against us renders an honest empty page (200, not 404): the
# URL space is our own history, so "no engagements with us" is a valid answer, not a missing
# resource. Reuses the member/alliance-pilot audience of the analytics dashboard.
def _adversary_page(request: HttpRequest, kind: str, entity_id: int) -> HttpResponse:
    from apps.corporation.models import EveName

    from . import adversary
    from .models import WatchlistEntry

    if not _can_view_stats(request.user):
        return render(request, "killboard/adversary.html", {"denied": True}, status=403)

    profile = adversary.adversary_profile(kind, entity_id)
    name = (
        EveName.objects.filter(entity_id=entity_id).values_list("name", flat=True).first()
        or f"#{entity_id}"
    )
    # Watchlist integration: which watchlists already carry this entity (every member sees the
    # "watched" state); officers additionally get the add-to-watchlist control, since watchlist
    # management is officer-tier (matching watchlist_add_entry).
    watched_in = list(
        WatchlistEntry.objects.filter(entity_type=kind, entity_id=entity_id)
        .values_list("watchlist__name", flat=True)
    )
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    # KB-27 (WS-D4): the inferred combat profile — playstyle / FC-likelihood / role usage — shares
    # the classifier + cache with recruitment and mentorship. Per-character only (the classifier is
    # defined over one pilot); a corp/alliance page aggregates many pilots and has no single profile.
    from . import intel_inference

    intel = (
        intel_inference.character_intel(entity_id)
        if kind == "character" and profile["has_history"] else None
    )
    return render(request, "killboard/adversary.html", {
        "denied": False,
        "kind": kind,
        "entity_id": entity_id,
        "entity_name": name,
        "profile": profile,
        "intel": intel,
        "summary": profile["summary"],
        "recent": adversary.recent_engagements(kind, entity_id),
        "watched_in": watched_in,
        "is_watched": bool(watched_in),
        "can_manage": is_officer,
        "watchlists": list(Watchlist.objects.order_by("name").values("id", "name"))
        if is_officer else [],
        "home_corp_id": _home(),
        # Members viewing intel: entity names on this page (and the detail/battle pages) may
        # resolve to adversary/pilot links. See killboard/_entity_link.html.
        "intel_links": True,
    })


# The GET pages mirror the analytics dashboard audience (corp members + registered alliance
# pilots), gated by ``_can_view_stats`` inside ``_adversary_page`` — not a strict member-role,
# so an alliance pilot sees the intel too. The watch action below stays officer-tier.
@login_required
def adversary_character(request: HttpRequest, entity_id: int) -> HttpResponse:
    return _adversary_page(request, "character", entity_id)


@login_required
def adversary_corporation(request: HttpRequest, entity_id: int) -> HttpResponse:
    return _adversary_page(request, "corporation", entity_id)


@login_required
def adversary_alliance(request: HttpRequest, entity_id: int) -> HttpResponse:
    return _adversary_page(request, "alliance", entity_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def adversary_watch(request: HttpRequest, kind: str, entity_id: int) -> HttpResponse:
    """Officer: add this adversary entity to a watchlist (reuses the WatchlistEntry model).

    Adds to the chosen existing watchlist (or the first one, creating a default if the corp
    has none yet), then returns to the adversary page. Officer-tier, matching the existing
    watchlist add/remove views — members see only the "watched" state, not this action.
    """
    from . import adversary
    from .models import WatchlistEntry

    if not adversary.is_valid_kind(kind):
        raise Http404("Unknown entity kind.")
    raw = (request.POST.get("watchlist_id") or "").strip()
    watchlist = Watchlist.objects.filter(pk=int(raw)).first() if raw.isdigit() else None
    if watchlist is None:
        watchlist = (
            Watchlist.objects.order_by("name").first()
            or Watchlist.objects.create(name=gettext("Watchlist"))
        )
    _entry, created = WatchlistEntry.objects.get_or_create(
        watchlist=watchlist, entity_type=kind, entity_id=entity_id
    )
    if created:
        audit_log(request.user, "watchlist.add_entry", target_type="watchlist",
                  target_id=str(watchlist.pk),
                  metadata={"entity_type": kind, "entity_id": entity_id, "via": "adversary"},
                  ip=client_ip(request))
        messages.success(
            request, gettext("Added to watchlist “%(name)s”.") % {"name": watchlist.name}
        )
    else:
        messages.info(
            request, gettext("Already on watchlist “%(name)s”.") % {"name": watchlist.name}
        )
    return redirect(f"killboard:adversary_{kind}", entity_id=entity_id)


# --- KB-34 D-scan / Local paste analyzer (WS-C4) ------------------------------
# Member-gated (the intel audience: corp members + registered alliance pilots, via
# ``_can_view_stats``). Fully STATELESS: a paste is analysed in-request and never persisted —
# no model, no migration. The analyse POST fans out to ESI (name/affiliation resolution) so it
# is rate-limited per user. Broadcasting the one-click alert is a corp-member action.
@login_required
def scan_analyzer(request: HttpRequest) -> HttpResponse:
    from . import scan_analyzer as scan

    if not _can_view_stats(request.user):
        return render(request, "killboard/scan.html", {"denied": True}, status=403)

    ctx: dict = {
        "denied": False, "raw": "", "system": "", "analysis": None,
        "recommendations": None, "kind": None, "too_large": False,
        "rate_limited": False, "did_analyze": False, "alert_sent": False,
        "is_member": rbac.has_role(request.user, rbac.ROLE_MEMBER),
    }
    if request.method != "POST":
        return render(request, "killboard/scan.html", ctx)

    raw = request.POST.get("paste", "") or ""
    system = (request.POST.get("system", "") or "").strip()
    want_alert = bool(request.POST.get("send_alert"))
    ctx["raw"] = raw
    ctx["system"] = system

    if not scan.rate_limit_ok(request.user.id):
        ctx["rate_limited"] = True
        return render(request, "killboard/scan.html", ctx, status=429)

    try:
        parsed = scan.parse(raw)
    except scan.PasteTooLarge:
        ctx["too_large"] = True
        return render(request, "killboard/scan.html", ctx, status=413)

    ctx["kind"] = parsed["kind"]
    if parsed["kind"] == scan.ScanKind.DSCAN:
        analysis = scan.analyze_dscan(parsed["rows"])
        ctx["recommendations"] = scan.recommend_counter_doctrines(analysis)
        has_content = analysis["ship_count"] > 0 or analysis["unmatched"] > 0
    else:
        analysis = scan.analyze_local(parsed["names"])
        has_content = analysis["pilot_count"] > 0 or bool(analysis["unresolved"])
    ctx["analysis"] = analysis
    ctx["did_analyze"] = True
    audit_log(request.user, "killboard.scan_analyze", target_type="killboard_scan",
              metadata={"kind": parsed["kind"]}, ip=client_ip(request))

    if want_alert and has_content:
        if not ctx["is_member"]:
            messages.info(request, gettext("Only corporation members can broadcast an intel alert."))
        else:
            alert = scan.emit_alert(analysis, system=system, source_id=f"scan:{request.user.id}")
            ctx["alert_sent"] = True
            if alert is not None:
                messages.success(request, gettext("Intel alert broadcast to the corp."))
            else:
                messages.info(
                    request,
                    gettext("Analysis done, but no alert channel is armed to broadcast it."),
                )
            audit_log(request.user, "killboard.scan_alert", target_type="killboard_scan",
                      metadata={"kind": parsed["kind"]}, ip=client_ip(request))

    return render(request, "killboard/scan.html", ctx)


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
            # KB-33: each watched entity links to its adversary page (members only).
            "intel_links": True,
            "home_corp_id": _home(),
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

    from . import battle_sides, roles

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

    # KB-36: per-side battle-role composition ("2 logi vs their 5"). Victims are classified
    # from their fitted modules (item-based); attackers from a hull approximation. Built once
    # from the detected side membership, then attached to each side below.
    side_of_entity = {
        (m.entity_type, m.entity_id): s.index for s in sides for m in s.members.all()
    }
    role_comp = roles.battle_role_composition(report, side_of_entity)

    side_views = []
    move_targets = [(s.index, s.label) for s in sides]
    for s in sides:
        members = [
            {
                "entity_type": m.entity_type, "entity_id": m.entity_id,
                "name": entity_names.get(m.entity_id) or f"{m.entity_type} {m.entity_id}",
                "kills": m.kills, "losses": m.losses, "isk_lost": m.isk_lost,
                "is_manual": m.is_manual,
                # KB-33: the home corporation itself is "us" (no adversary link); everyone
                # else on any side links to their adversary page.
                "is_home": bool(
                    m.entity_type == "corporation" and m.entity_id == _home()
                ),
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
            "roles": role_comp.get(s.index, []),
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
        # KB-33: side members link to adversary pages — but only for members, and never on the
        # public share page (an anonymous permalink viewer keeps plain names).
        "intel_links": bool(not public and rbac.has_role(request.user, rbac.ROLE_MEMBER)),
        "home_corp_id": _home(),
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


# --- KB-32 combat campaigns (WS-C2) ------------------------------------------
# Access map. List + detail (pk): member-gated. Public slug page: anonymous when the
# campaign is PUBLIC (core scoreboard only). Create/edit/delete: officer-gated + audited.
# The public list stays MEMBER-only on purpose — a public campaign is reachable by its
# shareable slug link, not by being enumerated to anonymous visitors.
def _parse_campaign_scope(request: HttpRequest) -> dict:
    """A validated scope dict from the officer campaign form's fields.

    Multi-value inputs mirror killfeed_config (sec-band checkboxes, id lists); entity
    id lists accept comma/space-separated integers. Unknown tokens are dropped so a
    hand-typed field can't inject junk into the stored scope.
    """
    from apps.doctrines.models import Doctrine

    from . import killfeed_rules

    def _ids(field: str) -> list[int]:
        raw = (request.POST.get(field) or "").replace(",", " ").split()
        return [int(tok) for tok in raw if tok.isdigit()]

    direction = request.POST.get("direction")
    if direction not in ("kills", "losses", "both"):
        direction = "both"
    entity_side = request.POST.get("entity_side")
    if entity_side not in ("victim", "attacker", "either"):
        entity_side = "either"

    valid_bands = {v for v, _label in killfeed_rules.SEC_BANDS}
    doctrine_ids = [int(d) for d in request.POST.getlist("doctrine_ids") if d.isdigit()]
    valid_docs = set(
        Doctrine.objects.filter(id__in=doctrine_ids).values_list("id", flat=True)
    )
    return {
        "direction": direction,
        "entity_side": entity_side,
        "system_ids": _ids("system_ids"),
        "region_ids": _ids("region_ids"),
        "sec_bands": [b for b in request.POST.getlist("sec_bands") if b in valid_bands],
        "doctrine_ids": [d for d in doctrine_ids if d in valid_docs],
        "character_ids": _ids("character_ids"),
        "corporation_ids": _ids("corporation_ids"),
        "alliance_ids": _ids("alliance_ids"),
    }


def _parse_dt(raw: str | None):
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    raw = (raw or "").strip()
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _apply_campaign_post(request: HttpRequest, campaign) -> str | None:
    """Validate + apply the campaign form POST onto ``campaign``. Returns an error
    string on failure (nothing persisted), or ``None`` on success (caller saves)."""
    from decimal import Decimal, InvalidOperation

    from apps.operations.models import Operation

    from .models import CombatCampaign

    name = (request.POST.get("name") or "").strip()
    if not name:
        return gettext("Give the campaign a name.")
    start = _parse_dt(request.POST.get("start_time"))
    if start is None:
        return gettext("Pick a valid start date and time.")
    end = _parse_dt(request.POST.get("end_time"))
    if end is not None and end < start:
        return gettext("The end must be on or after the start (leave it blank for an ongoing campaign).")

    visibility = request.POST.get("visibility")
    if visibility not in CombatCampaign.Visibility.values:
        visibility = CombatCampaign.Visibility.MEMBER

    budget = None
    raw_budget = (request.POST.get("srp_budget_isk") or "").replace(",", "").strip()
    if raw_budget:
        try:
            budget = max(Decimal("0"), Decimal(raw_budget))
        except InvalidOperation:
            return gettext("Enter the SRP budget as a number, or leave it blank.")

    target = None
    raw_target = (request.POST.get("doctrine_target_pct") or "").strip()
    if raw_target:
        if not raw_target.isdigit() or int(raw_target) > 100:
            return gettext("The doctrine target must be a whole percentage from 0 to 100.")
        target = int(raw_target)

    operation = None
    raw_op = (request.POST.get("operation") or "").strip()
    if raw_op.isdigit():
        operation = Operation.objects.filter(pk=int(raw_op)).first()

    campaign.name = name
    campaign.description = (request.POST.get("description") or "").strip()
    campaign.start_time = start
    campaign.end_time = end
    campaign.visibility = visibility
    campaign.is_active = request.POST.get("is_active") == "1"
    campaign.srp_budget_isk = budget
    campaign.doctrine_target_pct = target
    campaign.operation = operation
    campaign.scope = _parse_campaign_scope(request)
    return None


def _campaign_form_context(request: HttpRequest, campaign) -> dict:
    from apps.doctrines.models import Doctrine
    from apps.operations.models import Operation

    from . import killfeed_rules
    from .models import CombatCampaign

    return {
        "campaign": campaign,
        "editing": bool(campaign and campaign.pk),
        "scope": (campaign.scope if campaign else {}) or {},
        "visibility_choices": CombatCampaign.Visibility.choices,
        "sec_bands": killfeed_rules.SEC_BANDS,
        "doctrines": list(Doctrine.objects.order_by("name").values("id", "name")),
        "operations": list(
            Operation.objects.filter(target_at__isnull=False)
            .exclude(status__in=[Operation.Status.DRAFT])
            .order_by("-target_at")
            .values("id", "name")[:100]
        ),
        "directions": [
            ("both", gettext("Kills & losses")),
            ("kills", gettext("Kills only")),
            ("losses", gettext("Losses only")),
        ],
        "entity_sides": [
            ("either", gettext("Either side")),
            ("victim", gettext("As victim (our kills against them)")),
            ("attacker", gettext("As attacker (their gang, our losses)")),
        ],
    }


def _campaign_context(request: HttpRequest, campaign, *, public: bool) -> dict:
    """Shared render context for the member (pk) and public (slug) campaign pages.

    The SRP budget/spend and doctrine-compliance overlays are FitDeviation/SRP-derived
    (sensitive), so they are exposed only to officers — an anonymous/public or a
    below-officer member viewer never receives them. The core scoreboard (kills, losses,
    ISK, efficiency, top pilots/ships, participation, recent feed) is public.
    """
    from . import combat_campaigns

    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    stats = combat_campaigns.campaign_stats(campaign)
    return {
        "campaign": campaign,
        "public": public,
        "stats": stats,
        "top_pilots": stats["top_pilots_by_main"],
        "top_ships": stats["top_ships"],
        "recent": combat_campaigns.recent_matches(campaign),
        "srp": stats["srp"] if is_officer else None,
        "compliance": stats["compliance"] if is_officer else None,
        "compliance_delta": stats["compliance_delta"] if is_officer else None,
        "doctrine_target_pct": stats["doctrine_target_pct"] if is_officer else None,
        "is_officer": is_officer,
        "can_manage": is_officer and not public,
        "operation": campaign.operation,
        "permalink": request.build_absolute_uri(
            reverse("killboard:campaign_public", args=[campaign.slug])
        ),
    }


@login_required
@role_required(rbac.ROLE_MEMBER)
def combat_campaigns_list(request: HttpRequest) -> HttpResponse:
    """Member list of every combat campaign (public ones are also here for members;
    anonymous visitors reach a public campaign by its slug link, not this list)."""
    from . import combat_campaigns
    from .models import CombatCampaign

    rows = []
    for c in CombatCampaign.objects.all():
        s = combat_campaigns.campaign_stats(c)
        rows.append({
            "campaign": c, "kills": s["kills"], "losses": s["losses"],
            "isk_destroyed": s["isk_destroyed"], "isk_lost": s["isk_lost"],
            "efficiency": round(s["efficiency"]),
        })
    return render(request, "killboard/campaigns.html", {"rows": rows})


@login_required
@role_required(rbac.ROLE_MEMBER)
def combat_campaign_detail(request: HttpRequest, pk: int) -> HttpResponse:
    from .models import CombatCampaign

    campaign = get_object_or_404(CombatCampaign, pk=pk)
    return render(request, "killboard/campaign_detail.html",
                  _campaign_context(request, campaign, public=False))


def combat_campaign_public(request: HttpRequest, slug: str) -> HttpResponse:
    """Anonymous-viewable permalink for a PUBLIC campaign.

    A member-only campaign 404s here so its existence isn't leaked; members reach it at
    the member-gated pk URL instead. Officer overlays stay suppressed for anyone below
    officer (an anon viewer always is), so the shared page carries no SRP/compliance data.
    """
    from .models import CombatCampaign

    campaign = get_object_or_404(
        CombatCampaign, slug=slug, visibility=CombatCampaign.Visibility.PUBLIC
    )
    return render(request, "killboard/campaign_detail.html",
                  _campaign_context(request, campaign, public=True))


@login_required
@role_required(rbac.ROLE_OFFICER)
def combat_campaign_create(request: HttpRequest) -> HttpResponse:
    from .models import CombatCampaign

    if request.method == "POST":
        campaign = CombatCampaign(created_by=request.user)
        err = _apply_campaign_post(request, campaign)
        if err:
            messages.error(request, err)
            return redirect("killboard:campaign_create")
        campaign.save()
        audit_log(request.user, "combat_campaign.create", target_type="combat_campaign",
                  target_id=str(campaign.pk), metadata={"name": campaign.name},
                  ip=client_ip(request))
        messages.success(request, gettext("Campaign created."))
        return redirect("killboard:campaign_detail", pk=campaign.pk)
    return render(request, "killboard/campaign_form.html",
                  _campaign_form_context(request, None))


@login_required
@role_required(rbac.ROLE_OFFICER)
def combat_campaign_edit(request: HttpRequest, pk: int) -> HttpResponse:
    from .models import CombatCampaign

    campaign = get_object_or_404(CombatCampaign, pk=pk)
    if request.method == "POST":
        err = _apply_campaign_post(request, campaign)
        if err:
            messages.error(request, err)
            return redirect("killboard:campaign_edit", pk=pk)
        campaign.save()
        audit_log(request.user, "combat_campaign.edit", target_type="combat_campaign",
                  target_id=str(campaign.pk), metadata={"name": campaign.name},
                  ip=client_ip(request))
        messages.success(request, gettext("Campaign updated."))
        return redirect("killboard:campaign_detail", pk=pk)
    return render(request, "killboard/campaign_form.html",
                  _campaign_form_context(request, campaign))


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def combat_campaign_delete(request: HttpRequest, pk: int) -> HttpResponse:
    from .models import CombatCampaign

    campaign = get_object_or_404(CombatCampaign, pk=pk)
    audit_log(request.user, "combat_campaign.delete", target_type="combat_campaign",
              target_id=str(pk), metadata={"name": campaign.name}, ip=client_ip(request))
    campaign.delete()
    messages.success(request, gettext("Campaign deleted."))
    return redirect("killboard:campaigns")


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


# --------------------------------------------------------------------------- #
#  KB-38 self-host adoption profile (WS-D5): the setup wizard + history import +
#  branding. The wizard is DIRECTOR-gated (it exposes onboarding + branding for the
#  whole instance); the history-import launcher is officer-or-director.
# --------------------------------------------------------------------------- #
def _setup_context(request) -> dict:
    from . import branding, setup_status
    from .models import KillboardHistoryImport

    active = KillboardHistoryImport.active()
    # ``home_corp_name`` is intentionally NOT set here — the roles context processor already
    # provides it, and overriding it with a view value would risk shadowing the real name.
    return {
        "steps": setup_status.wizard_steps(request, _home(), active_import=active),
        "active_import": active,
        "recent_imports": list(KillboardHistoryImport.objects.all()[:5]),
        "branding": branding.get_branding(),
        "sources": KillboardHistoryImport.Source.choices,
    }


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def killboard_setup(request: HttpRequest) -> HttpResponse:
    """DIRECTOR: the self-host setup wizard — a live status page for standing up the board."""
    return render(request, "killboard/setup.html", _setup_context(request))


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def killboard_history_import_start(request: HttpRequest) -> HttpResponse:
    """Officer/director: enqueue a one-click history import. Refuses a concurrent run."""
    import datetime as _dt

    from . import tasks
    from .models import KillboardHistoryImport

    source = request.POST.get("source") or KillboardHistoryImport.Source.EVEREF
    if source not in KillboardHistoryImport.Source.values:
        messages.error(request, gettext("Unknown import source."))
        return redirect("killboard:setup")

    if KillboardHistoryImport.active() is not None:
        messages.info(request, gettext("A history import is already running — wait for it to "
                                       "finish or cancel it before starting another."))
        return redirect("killboard:setup")

    def _date(field):
        raw = (request.POST.get(field) or "").strip()
        if not raw:
            return None
        try:
            return _dt.datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None

    job = KillboardHistoryImport.objects.create(
        source=source,
        from_date=_date("from_date") if source == KillboardHistoryImport.Source.EVEREF else None,
        to_date=_date("to_date") if source == KillboardHistoryImport.Source.EVEREF else None,
        created_by=request.user,
    )
    audit_log(request.user, "killboard.history_import_start",
              target_type="killboard_history_import", target_id=str(job.pk),
              metadata={"source": source}, ip=client_ip(request))
    tasks.run_history_import.delay(job.pk)
    messages.success(request, gettext("History import started."))
    return redirect("killboard:setup")


@login_required
@role_required(rbac.ROLE_OFFICER)
def killboard_history_import_status(request: HttpRequest) -> HttpResponse:
    """htmx poll fragment: the current/last import's progress."""
    from .models import KillboardHistoryImport

    active = KillboardHistoryImport.active()
    job = active or KillboardHistoryImport.objects.order_by("-created_at").first()
    return render(request, "killboard/_import_status.html",
                  {"active_import": active, "job": job})


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def killboard_history_import_cancel(request: HttpRequest) -> HttpResponse:
    """Officer/director: best-effort cancel of the running import (honoured between batches)."""
    from .models import KillboardHistoryImport

    active = KillboardHistoryImport.active()
    if active is not None:
        active.cancel_requested = True
        active.save(update_fields=["cancel_requested"])
        audit_log(request.user, "killboard.history_import_cancel",
                  target_type="killboard_history_import", target_id=str(active.pk),
                  ip=client_ip(request))
        messages.info(request, gettext("Cancellation requested — the import will stop after the "
                                       "current batch."))
    return redirect("killboard:setup")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def killboard_branding(request: HttpRequest) -> HttpResponse:
    """DIRECTOR: save the corp branding overlay (name/logo/accent/tagline)."""
    from . import branding

    _clean, errors = branding.set_branding(
        {
            "display_name": request.POST.get("display_name", ""),
            "logo_url": request.POST.get("logo_url", ""),
            "accent_color": request.POST.get("accent_color", ""),
            "footer_tagline": request.POST.get("footer_tagline", ""),
        },
        user=request.user,
    )
    if errors:
        for msg in errors:
            messages.error(request, msg)
    else:
        audit_log(request.user, "killboard.branding_save", target_type="killboard_branding",
                  ip=client_ip(request))
        messages.success(request, gettext("Branding saved."))
    return redirect("killboard:setup")
