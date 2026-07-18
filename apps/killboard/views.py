"""Killboard views: public corp killboard, plus intel watchlists and battle reports."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Page, Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from apps.sde.models import SdeSolarSystem
from apps.sde.search import search_systems
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .battle import generate_battle_report
from .forms import BattleReportForm, WatchlistEntryForm, WatchlistForm
from .intel import watchlist_overview
from .models import BattleReport, Killmail, KillmailParticipant, Watchlist, WatchlistEntry

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
    template = "killboard/_feed.html" if request.headers.get("HX-Request") else "killboard/list.html"
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
        },
    )


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

    year_raw = (request.GET.get("year") or "").strip()
    month_raw = (request.GET.get("month") or "").strip()
    historical = year_raw.isdigit() and min_rank_year <= int(year_raw) <= now_year
    sel_year = sel_month = None
    prev_period = next_period = None

    if historical:
        sel_year = int(year_raw)
        if month_raw.isdigit() and 1 <= int(month_raw) <= 12:
            sel_month = int(month_raw)
        data = aggregation.historical_leaderboards(sel_year, sel_month)
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
        data = leaderboards(window_key)

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
    # Fit deviation is sensitive (it implies a mistake): show it only to the pilot
    # who lost the ship or to an officer — never to peers (SECURITY / PRD §B5).
    deviation = getattr(killmail, "fit_deviation", None)
    if deviation is not None and not deviation.is_clean:
        viewer_is_owner = (
            request.user.is_authenticated
            and killmail.victim_character_id
            and request.user.characters.filter(character_id=killmail.victim_character_id).exists()
        )
        if not (viewer_is_owner or rbac.has_role(request.user, rbac.ROLE_OFFICER)):
            deviation = None
    else:
        deviation = None
    return render(
        request,
        "killboard/detail.html",
        {
            "killmail": killmail,
            "attackers": killmail.participants.filter(role="attacker").order_by("-damage_done"),
            "deviation": deviation,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def system_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the battle-report system picker."""
    return JsonResponse(search_systems(request.GET.get("q", ""), limit=20), safe=False)


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
@login_required
@role_required(rbac.ROLE_MEMBER)
def battle_report_detail(request: HttpRequest, pk: int) -> HttpResponse:
    report = get_object_or_404(BattleReport, pk=pk)
    from apps.corporation.models import EveName
    from apps.sde.models import SdeType

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
    return render(request, "killboard/battle_report.html", {
        "report": report, "sides_chart": sides_chart, "ships_chart": ships_chart,
    })


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

        cfg.min_loss_value = _dec("min_loss_value", cfg.min_loss_value)
        cfg.min_kill_value = _dec("min_kill_value", cfg.min_kill_value)
        cfg.save(update_fields=["enabled", "min_loss_value", "min_kill_value", "updated_at"])
        messages.success(request, gettext("Kill-feed settings saved."))
        return redirect("killboard:killfeed_config")
    return render(
        request,
        "killboard/killfeed_config.html",
        {"cfg": cfg, "ks": ks, "ingest": ingest_status()},
    )
