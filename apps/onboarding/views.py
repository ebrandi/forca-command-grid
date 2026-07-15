"""Onboarding views: the new-player dashboard (works before and after login)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from core import pilots

from .models import GlossaryTerm, OnboardingMilestone, OnboardingProgress
from .services import evaluate_milestones, is_manual, next_actions

# The journey phases, in the order a new pilot walks them. Icons are sprite ids
# from templates/_icons.html; blurbs set expectations before the checklist.
PHASES = [
    ("account", _("Get connected"), "#i-key",
     _("Link your characters and plug into corp comms — this is how we find you and how the Grid briefs you.")),
    ("skills", _("Show us your skills"), "#i-rookie",
     _("Share your skill sheet so every tool here can answer 'can I fly this?' honestly.")),
    ("doctrine", _("Get combat-ready"), "#i-ship",
     _("Train into a corp doctrine ship. Flying what the FC calls for is the single biggest thing you can do.")),
    ("activity", _("Live here"), "#i-bolt",
     _("Move in, undock with us, and start earning — nullsec pays the pilots who show up.")),
]


def onboarding_dashboard(request: HttpRequest) -> HttpResponse:
    glossary = GlossaryTerm.objects.all()
    actions: list = []
    character = None
    phases: list[dict] = []
    done_count = total_count = 0
    if request.user.is_authenticated:
        character = pilots.acting_pilot(request.user)  # LP-3: the pilot the user is FLYING, not the account's main.
        if character:
            evaluate_milestones(character)
            progress_by_id = {
                p.milestone_id: p
                for p in OnboardingProgress.objects.filter(character=character)
            }
            milestones = list(OnboardingMilestone.objects.filter(active=True))
            rows_by_cat: dict[str, list[dict]] = {}
            for m in milestones:
                p = progress_by_id.get(m.id)
                row = {
                    "id": m.id,
                    "title": m.title_i18n,
                    "description": m.description_i18n,
                    "url": m.url,
                    "done": bool(p and p.status == OnboardingProgress.Status.DONE),
                    "manual": is_manual(m.criteria),
                    "auto_detected": bool(p and p.auto_detected),
                }
                rows_by_cat.setdefault(m.category, []).append(row)
                total_count += 1
                done_count += row["done"]
            for key, label, icon, blurb in PHASES:
                rows = rows_by_cat.pop(key, [])
                if rows:
                    phases.append({
                        "key": key, "label": label, "icon": icon, "blurb": blurb,
                        "rows": rows, "done": sum(1 for r in rows if r["done"]),
                    })
            # Categories outside the canonical order (defensive) still render.
            for key, rows in rows_by_cat.items():
                phases.append({
                    "key": key, "label": key.title(), "icon": "#i-check", "blurb": "",
                    "rows": rows, "done": sum(1 for r in rows if r["done"]),
                })
            actions = next_actions(character)
    return render(
        request,
        "onboarding/dashboard.html",
        {
            "glossary": glossary,
            "actions": actions,
            "phases": phases,
            "done_count": done_count,
            "total_count": total_count,
            "pct_done": round(done_count / total_count * 100) if total_count else 0,
            "character": character,
        },
    )


@login_required
@require_POST
def milestone_action(request: HttpRequest, pk: int) -> HttpResponse:
    """The pilot checks off (or un-checks) a MANUAL milestone for themselves.

    Auto-detected milestones are engine-owned and can't be toggled by hand —
    the button never renders for them, and the guard here backs that up.
    """
    milestone = get_object_or_404(OnboardingMilestone, pk=pk, active=True)
    character = (
        request.user.characters.filter(is_main=True).first()
        or request.user.characters.first()
    )
    if character is None or not is_manual(milestone.criteria):
        return redirect("onboarding:dashboard")
    progress, _ = OnboardingProgress.objects.get_or_create(
        character=character, milestone=milestone
    )
    if request.POST.get("action") == "undo":
        progress.status = OnboardingProgress.Status.TODO
        progress.completed_at = None
    else:
        progress.status = OnboardingProgress.Status.DONE
        progress.completed_at = timezone.now()
    progress.auto_detected = False
    progress.save(update_fields=["status", "completed_at", "auto_detected"])
    return redirect("onboarding:dashboard")
