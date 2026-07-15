"""Knowledge base: read (visibility-gated), author (officer/mentor)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .models import KbPage, KbRevision
from .render import render_markdown
from .services import can_view, make_resolver, visible_pages


def kb_list(request: HttpRequest) -> HttpResponse:
    # Not @login_required: a prospect (anonymous, or a logged-in non-member recruit) may
    # read the public tier. ``visible_pages`` restricts anyone without a member role to
    # PUBLIC pages, so the visibility gate — not the login gate — is the single control.
    pages = visible_pages(request.user).order_by("category", "title")
    by_cat: dict[str, list] = {}
    for page in pages:
        by_cat.setdefault(page.category or _("General"), []).append(page)
    return render(
        request,
        "kb/list.html",
        {
            "categories": sorted(by_cat.items()),
            "can_author": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        },
    )


def kb_detail(request: HttpRequest, slug: str) -> HttpResponse:
    # Not @login_required: public-tier pages are readable by prospects; ``can_view``
    # still 403s member/officer tiers for anyone without the role (incl. anonymous).
    page = get_object_or_404(KbPage, slug=slug)
    if not can_view(request.user, page):
        # Anonymous prospects get a 404 (a gated page is indistinguishable from a missing
        # one, so opening the public tier doesn't leak which member/officer slugs exist);
        # a logged-in member still gets a 403 so they know a higher-tier page is there.
        if not getattr(request.user, "is_authenticated", False):
            raise Http404
        return render(request, "doctrines/forbidden.html", status=403)
    return render(
        request,
        "kb/detail.html",
        {
            "page": page,
            "rendered": render_markdown(page.body_md, make_resolver(request.user)),
            "can_author": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        },
    )


@login_required
@role_required(rbac.ROLE_OFFICER)
def kb_edit(request: HttpRequest, slug: str | None = None) -> HttpResponse:
    page = get_object_or_404(KbPage, slug=slug) if slug else None
    return render(request, "kb/edit.html", {"page": page, "visibilities": KbPage.Visibility.choices})


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def kb_save(request: HttpRequest, slug: str | None = None) -> HttpResponse:
    title = (request.POST.get("title") or "").strip()
    body = request.POST.get("body_md") or ""
    visibility = request.POST.get("visibility")
    if visibility not in KbPage.Visibility.values:
        visibility = KbPage.Visibility.MEMBER
    if not title:
        messages.error(request, _("A page needs a title."))
        return redirect("kb:list")

    if slug:
        page = get_object_or_404(KbPage, slug=slug)
        page.title = title
        page.category = (request.POST.get("category") or "").strip()
        page.visibility = visibility
        page.body_md = body
        page.save()
    else:
        base = slugify(title)[:80] or "page"
        unique = base
        i = 2
        while KbPage.objects.filter(slug=unique).exists():
            unique = f"{base}-{i}"
            i += 1
        page = KbPage.objects.create(
            slug=unique, title=title, category=(request.POST.get("category") or "").strip(),
            visibility=visibility, body_md=body, created_by=request.user,
        )
    KbRevision.objects.create(page=page, body_md=body, edited_by=request.user)
    audit_log(request.user, "kb.saved", target_type="kb_page", target_id=str(page.pk), ip=client_ip(request))
    messages.success(request, _("Saved: %(title)s") % {"title": page.title})
    return redirect("kb:detail", slug=page.slug)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def kb_delete(request: HttpRequest, slug: str) -> HttpResponse:
    page = get_object_or_404(KbPage, slug=slug)
    page.delete()
    messages.success(request, _("Page deleted."))
    return redirect("kb:list")
