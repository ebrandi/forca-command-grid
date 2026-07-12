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
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import rbac

from . import eligibility as elig
from . import services, stats
from .draw import verify_draw
from .models import (
    RaffleContest,
    RaffleDraw,
    RaffleParticipantSummary,
    RaffleTicketLedgerEntry,
)
from .services import active_config


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
    return render(request, "raffle/detail.html", {
        "contest": contest,
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


def transparency(request, slug):
    """The public post-draw transparency report."""
    contest = _visible_or_404(slug, request.user)
    draw = contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True).first()
    if draw is None:
        raise Http404("No completed draw yet.")
    verification = verify_draw(draw)
    winners = list(draw.results.select_related("prize").order_by("draw_order"))
    # Disclose any earlier draws leadership discarded via redraw — the commit-reveal
    # proof only covers the FINAL draw, so a silent "redraw-until-win" would otherwise
    # defeat the fairness guarantee this page advertises.
    superseded_draws = list(
        contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=False)
        .order_by("created_at")
    )
    return render(request, "raffle/transparency.html", {
        "contest": contest,
        "draw": draw,
        "winners": winners,
        "verification": verification,
        "manifest": draw.manifest,
        "superseded_draws": superseded_draws,
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
