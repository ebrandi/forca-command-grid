"""Admin Console: Account recovery (officer character-detach / owner-hash reset).

The fail-closed SSO login guards deliberately refuse a character whose EVE owner hash
changed (sold/transferred) or was never captured (legacy row), telling the pilot to
"contact an officer to detach it" — but no such flow existed and Django /admin is
disabled, so recovery meant a raw DB edit. This gives officers an audited, least-
privilege recovery action instead.

Officer-gated. Guarded so an officer cannot detach a character linked to a Director or
Admin (only a Director+ can), preventing a detach from being used to disrupt leadership.
Never self-serviceable by the locked-out account (it can't sign in to reach this page).
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _as_cid(value):
    """A valid EVE character id (positive, within int64) or None — so a junk POST/GET
    value degrades to "not found" instead of a 500 on the BigIntegerField query."""
    s = str(value or "").strip()
    return int(s) if s.isdigit() and 0 < int(s) < 2**63 else None


def _character_row(ch) -> dict:
    """Display status for one character: who it's linked to, whether we hold an owner
    hash, and how many live tokens it has — the facts an officer needs to decide."""
    user = ch.user
    return {
        "character_id": ch.character_id,
        "name": ch.name or f"#{ch.character_id}",
        "linked_to": user.display_name if user else None,
        "linked_user_id": user.id if user else None,
        "is_director_linked": bool(user) and rbac.has_role(user, rbac.ROLE_DIRECTOR),
        "has_owner_hash": bool(ch.owner_hash),
        "is_corp_member": ch.is_corp_member,
        "live_tokens": ch.tokens.filter(revoked_at__isnull=True).count(),
    }


@login_required
@role_required(rbac.ROLE_OFFICER)
def character_recovery(request: HttpRequest) -> HttpResponse:
    """Search characters by id or name and show their link/ownership status."""
    from apps.sso.models import EveCharacter

    q = (request.GET.get("q") or "").strip()
    rows: list[dict] = []
    if q:
        cid = _as_cid(q)
        qs = EveCharacter.objects.select_related("user")
        qs = qs.filter(character_id=cid) if cid is not None else qs.filter(name__icontains=q)
        rows = [_character_row(ch) for ch in qs.order_by("name")[:25]]
    return render(request, "admin_audit/console/recovery.html", {
        "q": q,
        "rows": rows,
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def character_detach(request: HttpRequest) -> HttpResponse:
    """Detach a character (clear owner hash + unlink + revoke tokens). Audited."""
    from apps.sso.models import EveCharacter
    from apps.sso.services import detach_character

    q = (request.POST.get("q") or "").strip()
    reason = (request.POST.get("reason") or "").strip()
    cid = _as_cid(request.POST.get("character_id"))
    character = (
        EveCharacter.objects.select_related("user").filter(character_id=cid).first()
        if cid is not None else None
    )
    if character is None:
        messages.error(request, "That character was not found.")
        return redirect(f"{_recovery_url()}?{urlencode({'q': q})}")
    if not reason:
        messages.error(request, "A reason is required to detach a character (it is audited).")
        return redirect(f"{_recovery_url()}?{urlencode({'q': q or character.character_id})}")
    # Escalation guard: only a Director+ may detach a Director/Admin-linked character.
    target_user = character.user
    if (
        target_user is not None
        and rbac.has_role(target_user, rbac.ROLE_DIRECTOR)
        and not rbac.has_role(request.user, rbac.ROLE_DIRECTOR)
    ):
        messages.error(request, "Only a Director can detach a character linked to a Director.")
        return redirect(f"{_recovery_url()}?{urlencode({'q': q or character.character_id})}")

    result = detach_character(character, actor=request.user, reason=reason)
    audit_log(request.user, "recovery.character_detach.console", target_type="eve_character",
              target_id=str(character.character_id), ip=client_ip(request),
              metadata={"reason": reason})
    freed = " and revoked its tokens" if result["tokens_revoked"] else ""
    messages.success(
        request,
        f"Detached {character.name or character.character_id}{freed}. "
        "The owner can now re-link it at their next EVE login.",
    )
    return redirect(f"{_recovery_url()}?{urlencode({'q': q or character.character_id})}")


def _recovery_url() -> str:
    from django.urls import reverse

    return reverse("admin_audit:character_recovery")
