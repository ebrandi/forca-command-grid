"""Corporation roster: officer page listing members and registration status."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.rbac import role_required


@login_required
@role_required(rbac.ROLE_OFFICER)
def roster_view(request: HttpRequest) -> HttpResponse:
    from django.utils.dateparse import parse_datetime

    from apps.admin_audit.health import _last_sync

    from .roster import roster

    last = _last_sync("corp_members")
    return render(
        request,
        "corporation/roster.html",
        {
            **roster(),
            "last_sync": last,
            "last_sync_at": parse_datetime(last["at"]) if last and last.get("at") else None,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def extractions(request: HttpRequest) -> HttpResponse:
    """Member-facing moon-extraction calendar with countdowns."""
    import datetime as dt

    from django.utils import timezone

    from .models import MoonExtraction

    cutoff = timezone.now() - dt.timedelta(hours=6)  # keep just-popped chunks briefly
    rows = list(MoonExtraction.objects.filter(chunk_arrival__gte=cutoff))
    # 4.13: estimate each structure's ore composition + ISK/m³ from its recent mining
    # ledger so miners can self-select the richest chunk. One estimate per DISTINCT
    # structure, shared across its extractions (no N+1).
    from apps.mining.moon_value import compositions_for_structures
    comps = compositions_for_structures(r.structure_id for r in rows)
    for r in rows:
        r.composition = comps.get(r.structure_id)
    return render(request, "corporation/extractions.html", {
        "extractions": rows,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def sync_extractions(request: HttpRequest) -> HttpResponse:
    from .extractions import sync_moon_extractions

    result = sync_moon_extractions()
    if result["status"] == "ok":
        messages.success(request, _("Extractions synced — %(count)d scheduled.") % {"count": result["count"]})
    elif result["status"] == "no_token":
        messages.warning(request, _("No character has granted the corp-mining scope yet."))
    else:
        messages.error(request, _("Extraction sync failed; try again later."))
    return redirect("corporation:extractions")


@login_required
@role_required(rbac.ROLE_OFFICER)
def structures(request: HttpRequest) -> HttpResponse:
    """Director structures board: fuel countdowns, state and reinforcement timers."""
    from .models import CorpStructure

    rows = list(CorpStructure.objects.all())
    return render(request, "corporation/structures.html", {
        "structures": rows,
        "low_fuel": sum(1 for s in rows if s.is_low_fuel),
        "reinforced": sum(1 for s in rows if s.is_reinforced),
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def infrastructure(request: HttpRequest) -> HttpResponse:
    """CORP-2 (2.4): one urgency-ranked board of structure fuel, sov ADM and timers —
    home-defence health at a glance, merging the structures and sov pages."""
    from .infra import infrastructure_board

    items = infrastructure_board()
    return render(request, "corporation/infrastructure.html", {
        "items": items,
        "crit": sum(1 for i in items if i["severity"] == "critical"),
        "warn": sum(1 for i in items if i["severity"] == "warning"),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def sync_structures(request: HttpRequest) -> HttpResponse:
    from .structures_esi import sync_corp_structures

    result = sync_corp_structures()
    if result["status"] == "ok":
        messages.success(request, _("Structures synced — %(count)d tracked.") % {"count": result["count"]})
    elif result["status"] == "no_token":
        messages.warning(request, _("No character has granted the structure-monitoring scope yet."))
    else:
        messages.error(request, _("Structure sync failed; try again later."))
    return redirect("corporation:structures")


@login_required
@role_required(rbac.ROLE_MEMBER)
def standings(request: HttpRequest) -> HttpResponse:
    """Member-visible blue/red standings board (corp contacts)."""
    from .models import Contact

    contacts = list(Contact.objects.all())
    blue = [c for c in contacts if c.standing > 0]
    red = [c for c in contacts if c.standing < 0]
    return render(request, "corporation/standings.html", {
        "blue": blue, "red": red, "total": len(contacts),
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def sync_contacts(request: HttpRequest) -> HttpResponse:
    from .contacts import sync_corp_contacts

    result = sync_corp_contacts()
    if result["status"] == "ok":
        messages.success(request, _("Standings synced — %(count)d contacts.") % {"count": result["count"]})
    elif result["status"] == "no_token":
        messages.warning(request, _("No character has granted the corp-contacts scope yet."))
    else:
        messages.error(request, _("Standings sync failed; try again later."))
    return redirect("corporation:standings")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def finance(request: HttpRequest) -> HttpResponse:
    """Director/admin finance hub: balances, charts, KPIs, members and a forecast.

    One page over the corp wallet journal, with a window + division + forecast-
    horizon control. (Director gate also admits admin — admin outranks director.)
    """
    from .finance_analytics import (
        DEFAULT_HORIZON,
        DEFAULT_WINDOW,
        HORIZONS,
        WINDOWS,
        dashboard_cached,
        default_dashboard,
    )

    window = request.GET.get("window", DEFAULT_WINDOW)
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    horizon = request.GET.get("horizon", DEFAULT_HORIZON)
    if horizon not in HORIZONS:
        horizon = DEFAULT_HORIZON
    raw_div = request.GET.get("division") or ""
    division = int(raw_div) if raw_div.isdigit() else None

    if window == DEFAULT_WINDOW and division is None and horizon == DEFAULT_HORIZON:
        data = default_dashboard()  # warmed by a beat task
    else:
        data = dashboard_cached(window=window, division=division, horizon=horizon)
    return render(request, "corporation/finance.html", {"d": data})


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def income(request: HttpRequest) -> HttpResponse:
    """Legacy URL — folded into the unified finance hub."""
    return redirect("corporation:finance")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def sync_finance(request: HttpRequest) -> HttpResponse:
    from core.audit import audit_log, client_ip

    from .finance import sync_corp_wallets

    result = sync_corp_wallets()
    audit_log(request.user, "corp.finance_sync", target_type="corp", target_id="wallet",
              metadata={"status": result["status"]}, ip=client_ip(request))
    if result["status"] == "ok":
        messages.success(request, _("Wallet synced — %(entries)d journal entries.") % {"entries": result["entries"]})
    elif result["status"] == "no_token":
        messages.warning(request, _("No character has granted the corp-wallet scope yet."))
    else:
        messages.error(request, _("Wallet sync failed; try again later."))
    return redirect("corporation:finance")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def sync_roster(request: HttpRequest) -> HttpResponse:
    from .roster import import_corp_members

    result = import_corp_members()
    if result["status"] == "ok":
        messages.success(request, _("Roster synced — %(count)d members.") % {"count": result["count"]})
    elif result["status"] == "no_token":
        messages.warning(
            request,
            result["message"] + " " + _("Use “Grant member-tracking access” on the ESI Scopes page."),
        )
    elif result["status"] == "no_corp":
        messages.error(request, result["message"])
    else:
        messages.error(request, _("Sync failed: %(message)s") % {"message": result["message"]})
    return redirect("corporation:roster")
