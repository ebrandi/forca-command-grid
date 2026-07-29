"""Pilot-facing raffle views: the campaign dashboard, private performance, the
public transparency report and the archive.

Thin views — all data comes from :mod:`apps.raffle.stats` / model reads. The whole
namespace is gated by ``FeatureGateMiddleware`` (the ``raffle`` audience feature);
these views add login/ownership checks where a page shows personal data. No raffle
state is mutated here — that's the admin console.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import rbac

from . import eligibility as elig
from . import services, stats
from . import snapshot as pool_snapshot
from . import tickets as ticket_ids
from .draw import verify_draw
from .models import (
    RaffleContest,
    RaffleDraw,
    RaffleDrawResult,
    RaffleParticipantSummary,
    RaffleTicketLedgerEntry,
)
from .services import active_config

# How many ledger rows / pool rows a single page renders. A pilot with thousands of
# tickets must never make the server materialise or the browser paint the whole pool.
LEDGER_PAGE_SIZE = 50
POOL_PAGE_SIZE = 100


def _is_member(user) -> bool:
    """Corp-authenticated pilot. The pool roster is corp-visible, not world-visible."""
    return getattr(user, "is_authenticated", False) and rbac.has_role(user, rbac.ROLE_MEMBER)


def _is_officer(user) -> bool:
    return getattr(user, "is_authenticated", False) and rbac.has_role(user, rbac.ROLE_OFFICER)


def _visible_or_404(slug, user):
    contest = get_object_or_404(RaffleContest, slug=slug)
    if contest.status in RaffleContest.VISIBLE_STATUSES or _is_officer(user):
        return contest
    raise Http404("Contest not available.")


def home(request):
    """Landing: the active contest(s) featured, plus recent + archive links."""
    active = list(RaffleContest.objects.filter(status=RaffleContest.Status.ACTIVE))
    upcoming = list(
        RaffleContest.objects.filter(status=RaffleContest.Status.SCHEDULED).order_by("start_at")[:5]
    )
    recent = list(
        RaffleContest.objects.filter(
            status__in=[RaffleContest.Status.CLOSED, RaffleContest.Status.COMPLETED]
        ).order_by("-draw_at")[:5]
    )
    # If there's exactly one active contest and nothing else notable, go straight in.
    if len(active) == 1 and not upcoming:
        return redirect("raffle:detail", slug=active[0].slug)
    return render(request, "raffle/home.html", {
        "active": active, "upcoming": upcoming, "recent": recent,
        "config": active_config(),
        "adoption": stats.adoption_metrics(),
        "is_officer": _is_officer(request.user),
    })


def detail(request, slug):
    """The campaign dashboard for one contest."""
    contest = _visible_or_404(slug, request.user)
    from . import boosters
    booster = boosters.prize_booster_status(contest)
    activity = boosters.min_activity_status(contest)
    prizes = [
        {"prize": p,
         "effective": boosters.effective_prize_value(p, contest, achieved=booster["achieved"]),
         "boostable": p.prize_type in boosters.BOOSTABLE_PRIZE_TYPES,
         "boosted": booster["achieved"] and p.prize_type in boosters.BOOSTABLE_PRIZE_TYPES}
        for p in contest.prizes.order_by("rank")
    ]
    source_configs = [
        c for c in contest.source_configs.filter(enabled=True, visible_to_pilots=True)
    ]
    from .sources import get_source
    sources = [
        {"config": c, "source": get_source(c.source_key)}
        for c in source_configs
    ]

    my_elig = None
    my_summary = None
    if getattr(request.user, "is_authenticated", False):
        my_elig = elig.for_user(contest, request.user)
        my_summary = RaffleParticipantSummary.objects.filter(contest=contest, user=request.user).first()

    leaderboard = []
    if contest.leaderboard_visible:
        leaderboard = list(
            RaffleParticipantSummary.objects.filter(contest=contest, eligible=True)
            # -total_tickets hits the (contest, -total_tickets) index; rank is a
            # stable tiebreak. rank was assigned in ticket-desc order, so identical.
            .order_by("-total_tickets", "rank")[: contest.leaderboard_size]
        )

    recent_events = []
    if contest.show_recent_events:
        recent_events = list(
            RaffleTicketLedgerEntry.objects.filter(
                contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED
            ).order_by("-created_at")[:15]
        )

    winners = []
    draw = None
    superseded_draws = []
    if contest.status in (RaffleContest.Status.COMPLETED, RaffleContest.Status.ARCHIVED):
        draw = contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True).first()
        if draw:
            winners = list(draw.results.select_related("prize").order_by("draw_order"))
        # Transparency: if leadership redrew, the earlier (discarded) draws are shown
        # so a "redraw-until-win" can't hide behind the fairness proof of the final one.
        superseded_draws = list(
            contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=False)
            .order_by("created_at")
        )

    stat = stats.contest_statistics(contest)
    snap = pool_snapshot.current_snapshot(contest)
    my_odds = stats.win_chance(
        my_summary.total_tickets if my_summary else 0,
        stat["total_tickets"], len(prizes),
    )
    return render(request, "raffle/detail.html", {
        "contest": contest,
        "snapshot": snap,
        "my_odds": my_odds,
        "prizes": prizes,
        "activity": activity,
        "booster": booster,
        "sources": sources,
        "my_elig": my_elig,
        "my_summary": my_summary,
        "leaderboard": leaderboard,
        "recent_events": recent_events,
        "winners": winners,
        "draw": draw,
        "superseded_draws": superseded_draws,
        "stats": stat,
        "adoption": stat["adoption"],
        "config": active_config(),
        "is_officer": _is_officer(request.user),
    })


@login_required
def me(request, slug):
    """The pilot's own private performance page for a contest."""
    contest = _visible_or_404(slug, request.user)
    perf = stats.pilot_performance(contest, request.user)
    return render(request, "raffle/me.html", {
        "contest": contest,
        "perf": perf,
        "eligibility": perf["eligibility"],
        "config": active_config(),
    })


@login_required
def tickets(request, slug):
    """The pilot's own ticket ledger — every award, its number range and its evidence.

    Filtered and paginated in the database. A pilot with ten thousand tickets holds a few
    hundred award rows, and each row renders as a *range* (``#412–#511``) rather than as
    hundreds of cards, so the page cost is flat in the size of their history.
    """
    contest = _visible_or_404(slug, request.user)

    # Clamp every filter to a known domain rather than echoing what arrived. Besides the
    # obvious injection surface, an unclamped value silently returns an empty page and
    # reads to the pilot as "my tickets vanished".
    valid_sources = set(contest.source_configs.values_list("source_key", flat=True))
    source = request.GET.get("source", "").strip()
    source = source if source in valid_sources else ""
    status = request.GET.get("status", "").strip()
    status = status if status in RaffleTicketLedgerEntry.Status.values else ""

    raw_q = request.GET.get("q", "").strip()
    digits = raw_q.lstrip("#").strip()
    ticket_no = int(digits) if digits.isdigit() and len(digits) <= 9 else None

    qs = ticket_ids.pilot_entries(
        contest, request.user, source_key=source, status=status, ticket_no=ticket_no
    )
    page_obj = Paginator(qs, LEDGER_PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"entry": e, "evidence": ticket_ids.evidence_for(e)} for e in page_obj]

    # Which of my tickets actually entered the draw, once the pool is frozen.
    snap = pool_snapshot.current_snapshot(contest)
    my_pool_tickets = 0
    if snap is not None:
        my_pool_tickets = sum(row[3] for row in pool_snapshot.tickets_for_user(snap, request.user.pk))

    # A won ticket is the single most interesting row in the ledger — mark it.
    winning_entry_ids = set(
        RaffleDrawResult.objects
        .filter(draw__contest=contest, winner_user=request.user,
                winning_ledger_entry__isnull=False)
        .values_list("winning_ledger_entry_id", flat=True)
    )

    base_qs = "&".join(
        f"{k}={v}" for k, v in (("source", source), ("status", status),
                                ("q", digits if ticket_no is not None else "")) if v
    )
    return render(request, "raffle/tickets.html", {
        "contest": contest,
        "rows": rows,
        "page_obj": page_obj,
        "base_qs": base_qs,
        "filters": {"source": source, "status": status,
                    "q": digits if ticket_no is not None else ""},
        "sources": sorted(valid_sources),
        "statuses": RaffleTicketLedgerEntry.Status.choices,
        "perf": stats.pilot_performance(contest, request.user),
        "snapshot": snap,
        "my_pool_tickets": my_pool_tickets,
        "winning_entry_ids": winning_entry_ids,
        "config": active_config(),
    })


def pool(request, slug):
    """The frozen ticket pool a draw runs against — corp-visible, paginated.

    This is what makes the fairness claim checkable rather than merely asserted: the
    transparency page tells pilots to map the drawn offset onto the published pool, and
    before this view existed that pool was never published anywhere.

    Gated at member level. The hashes, totals and winning tickets stay public on the
    receipt; the per-pilot roster is corp business and is not put on the open internet.
    """
    contest = _visible_or_404(slug, request.user)
    snap = pool_snapshot.current_snapshot(contest)
    if snap is None:
        raise Http404("The ticket pool has not been frozen yet.")
    if not _is_member(request.user):
        return render(request, "raffle/pool_gated.html", {
            "contest": contest, "snapshot": snap,
        }, status=403)

    pilots = pool_snapshot.pilot_index(snap)
    page_obj = Paginator(snap.entries, POOL_PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [
        {"entry_id": r[0], "user_id": r[1], "ticket_start": r[2], "amount": r[3],
         "draw_start": r[4], "ticket_end": r[2] + r[3] - 1,
         "name": pilots.get(r[1], {}).get("name", ""),
         "character_id": pilots.get(r[1], {}).get("character_id")}
        for r in page_obj
    ]
    return render(request, "raffle/pool.html", {
        "contest": contest,
        "snapshot": snap,
        "rows": rows,
        "page_obj": page_obj,
        "verification": pool_snapshot.verify_snapshot(snap),
        "is_officer": _is_officer(request.user),
    })


def receipt_json(request, slug):
    """The draw receipt as machine-readable JSON.

    Everything needed to re-run the maths offline, and nothing that would leak account
    internals: no tokens, no internal notes, no leadership-only fields. The per-pilot pool
    roster is included only for corp members, matching :func:`pool`.
    """
    contest = _visible_or_404(slug, request.user)
    draw = contest.draws.filter(
        status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True
    ).first()
    if draw is None:
        raise Http404("No completed draw yet.")

    verification = verify_draw(draw)
    snap = draw.snapshot
    payload = {
        "format": "forca-raffle-receipt-v1",
        "contest": {"name": contest.name, "slug": contest.slug,
                    "start_at": contest.start_at.isoformat(),
                    "end_at": contest.end_at.isoformat(),
                    "draw_at": contest.draw_at.isoformat(),
                    "one_prize_per_pilot": contest.one_prize_per_pilot},
        "draw": {
            "drawn_at": draw.completed_at.isoformat() if draw.completed_at else None,
            "algorithm_version": draw.algorithm_version,
            "code_version": draw.code_version,
            "seed_commitment": draw.seed_commitment,
            "seed": draw.seed,
            "external_entropy": draw.external_entropy,
            "executed_by_automatically": draw.executed_by_id is None,
            "redraw_reason": draw.redraw_reason,
            "total_eligible_tickets": draw.total_eligible_tickets,
            "total_excluded_tickets": draw.total_excluded_tickets,
            "eligible_pilots": draw.eligible_pilots,
            "excluded_pilots": draw.excluded_pilots,
            "rolls": draw.random_values,
            "skipped": draw.skipped_draws,
        },
        "snapshot": None if snap is None else {
            "version": snap.version,
            "frozen_at": snap.frozen_at.isoformat(),
            "cutoff_at": snap.cutoff_at.isoformat() if snap.cutoff_at else None,
            "content_hash": snap.content_hash,
            "total_tickets": snap.total_tickets,
            "total_entries": snap.total_entries,
            "total_pilots": snap.total_pilots,
            "rules": snap.rules,
            "format": pool_snapshot.SNAPSHOT_FORMAT,
        },
        "winners": [
            {"prize_rank": r.prize.rank, "prize_name": r.prize.name,
             "pilot": r.winner_character_name,
             "character_id": r.winner_character_id,
             "winning_ticket": r.winning_ticket_no,
             "draw_offset": r.winning_ticket_index,
             "draw_order": r.draw_order,
             "status": r.status,
             "status_reason": r.status_reason,
             "replaces_draw_order": r.replaces.draw_order if r.replaces_id else None,
             "prize_delivered": r.fulfil_status == RaffleDrawResult.FulfilStatus.DELIVERED,
             "fulfil_status": r.fulfil_status}
            for r in draw.results.select_related("prize", "replaces").order_by("draw_order")
        ],
        "verification": {k: v for k, v in verification.items() if k != "snapshot"},
    }
    if snap is not None and _is_member(request.user):
        # The pool rows are what let a member recompute the content hash themselves.
        payload["snapshot"]["entries"] = snap.entries
        payload["snapshot"]["pilots"] = snap.pilots
    resp = JsonResponse(payload, json_dumps_params={"indent": 2})
    resp["Content-Disposition"] = f'inline; filename="{contest.slug}-draw-receipt.json"'
    return resp


def transparency(request, slug):
    """The public post-draw transparency report / draw receipt."""
    contest = _visible_or_404(slug, request.user)
    draw = contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True).first()
    if draw is None:
        raise Http404("No completed draw yet.")
    verification = verify_draw(draw)
    winners = list(
        draw.results.select_related("prize", "replaces", "winning_ledger_entry")
        .order_by("draw_order")
    )
    # Disclose any earlier draws leadership discarded via redraw — the commit-reveal
    # proof only covers the FINAL draw, so a silent "redraw-until-win" would otherwise
    # defeat the fairness guarantee this page advertises.
    superseded_draws = list(
        contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=False)
        .order_by("created_at")
    )
    # "Was one of MY tickets in this pool, and did it win?" — the question a pilot
    # actually arrives with. Answering it on the receipt is what turns the page from a
    # wall of hashes into something personally checkable.
    my_pool_tickets = 0
    my_result = None
    if getattr(request.user, "is_authenticated", False):
        if draw.snapshot_id:
            my_pool_tickets = sum(
                row[3] for row in pool_snapshot.tickets_for_user(draw.snapshot, request.user.pk)
            )
        my_result = next((w for w in winners if w.winner_user_id == request.user.pk), None)

    return render(request, "raffle/transparency.html", {
        "contest": contest,
        "draw": draw,
        "snapshot": draw.snapshot,
        "winners": winners,
        "verification": verification,
        "manifest": draw.manifest,
        "superseded_draws": superseded_draws,
        "my_pool_tickets": my_pool_tickets,
        "my_result": my_result,
        "is_member": _is_member(request.user),
        "is_officer": _is_officer(request.user),
    })


def archive(request):
    """Past contests the archive is allowed to show."""
    contests = list(
        RaffleContest.objects.filter(
            status__in=[RaffleContest.Status.COMPLETED, RaffleContest.Status.ARCHIVED],
            archive_public=True,
        ).order_by("-draw_at")
    )
    return render(request, "raffle/archive.html", {
        "contests": contests, "is_officer": _is_officer(request.user),
    })


@login_required
def outreach_opt_out(request):
    """RAF-3 (3.9): a pilot permanently opts out of enrolment-nudge DMs."""
    if request.method == "POST":
        services.opt_out_of_outreach(request.user)
        messages.success(request, _("Done — you won't be nudged about enrolling again."))
        return redirect("raffle:home")
    return render(request, "raffle/outreach_opt_out.html", {})
