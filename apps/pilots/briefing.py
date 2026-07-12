"""Daily briefings (PRD Module S, in-app).

A concise, prioritised digest composed from existing module outputs — it adds no
new "truth", so it can never disagree with the dashboards. Pilot and leadership
briefings are distinct; a pilot briefing only ever contains that pilot's own
data plus shareable corp asks.
"""
from __future__ import annotations

from django.utils.translation import gettext as _


def pilot_briefing(user) -> dict:
    """What changed / what to do, for one pilot — their own data only.

    Cached briefly per user: it composes several per-pilot skill computations
    (``closest_doctrines``/``highest_leverage_skill``) that cost ~1 s, and a
    "what to do today" digest tolerates a few minutes of staleness.
    """
    from django.core.cache import cache

    # v2: item kinds changed with the Daily Briefing merge (task_open split out of
    # task) — the version bump keeps stale pre-merge digests out of the new
    # partition logic across a deploy.
    cache_key = f"briefing:pilot:v2:{user.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from apps.operations.services import upcoming_for_pilot
    from apps.skills.gap import highest_leverage_skill
    from apps.skills.services import closest_doctrines
    from apps.srp.services import eligible_losses_for
    from apps.tasks.models import Task

    characters = list(user.characters.all())
    main = next((c for c in characters if c.is_main), characters[0] if characters else None)
    char_ids = [c.character_id for c in characters]

    items: list[dict] = []
    headline = None

    if main:
        op = upcoming_for_pilot(main)
        if op:
            msg = _("Prep for %(op)s: you're ready for %(ready)s/%(total)s doctrines.") % {
                "op": op["op"].name, "ready": op["ready"], "total": op["total"],
            }
            headline = {"kind": "operation", "text": msg, "url": f"/operations/{op['op'].pk}/"}

        lev = highest_leverage_skill(main)
        if lev:
            items.append({
                "kind": "train",
                "text": _("Train %(name)s %(level)s — unlocks %(count)s doctrine(s).") % {
                    "name": lev["name"], "level": lev["target_level"],
                    "count": lev["doctrine_count"],
                },
                "url": "/skills/",
            })

        close = closest_doctrines(main, limit=1)
        if close:
            items.append({
                "kind": "doctrine",
                "text": _("You're closest to flying %(doctrine)s.") % {"doctrine": close[0]["doctrine"]},
                "url": f"/doctrines/{close[0]['doctrine_id']}/readiness/",
            })

    srp = eligible_losses_for(char_ids, limit=5)
    if srp:
        from decimal import Decimal

        total = sum((e["payout"] for e in srp), start=Decimal("0"))
        items.append({
            "kind": "srp",
            "text": _("%(n)s loss(es) eligible for SRP (~%(isk)s ISK). Submit a claim.") % {
                "n": len(srp), "isk": f"{total:,.0f}",
            },
            "url": "/srp/",
        })

    open_tasks = Task.objects.filter(assignee=user).exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    ).count()
    claimable = Task.objects.filter(
        is_open=True, assignee__isnull=True, status=Task.Status.OPEN
    ).count()
    if open_tasks:
        items.append({"kind": "task", "text": _("You have %(n)s open task(s).") % {"n": open_tasks}, "url": "/tasks/"})
    elif claimable:
        # kind 'task_open' so the merged page can route it: it is a claimable-work
        # pointer, not a personal deadline — suppressed when the pick-up boards render.
        items.append(
            {"kind": "task_open", "text": _("%(n)s task(s) open to claim.") % {"n": claimable}, "url": "/tasks/"}
        )

    if headline is None and items:
        headline = items.pop(0)
    result = {"headline": headline, "items": items}
    cache.set(cache_key, result, 600)  # 10 min — a digest tolerates short staleness
    return result


# Kinds that are time-boxed: they expire (an op fires, SRP claims close, assigned
# work blocks someone). Everything else in the digest is evergreen advice.
_SIGNAL_KINDS = {"operation", "srp", "task"}


def partition_briefing(briefing: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Split the pilot digest into ``(signals, advice, claimable)``.

    The merged Daily Briefing's dedup contract: each digest row has exactly one
    home, decided by its nature. Signals (op prep, SRP money, tasks assigned to
    YOU) always render; advice (train/doctrine items) renders only when the
    ranked quest log is absent — the quest log is the canonical training advice;
    the claimable-tasks pointer renders only when the pick-up boards are absent.
    Enforced here in the view layer, never in the template, so the
    duplicate-advice confusion the merge removed cannot silently return.
    """
    rows = [it for it in [briefing.get("headline"), *briefing.get("items", [])] if it]
    signals = [it for it in rows if it.get("kind") in _SIGNAL_KINDS]
    claimable = [it for it in rows if it.get("kind") == "task_open"]
    advice = [it for it in rows if it.get("kind") not in _SIGNAL_KINDS and it.get("kind") != "task_open"]
    return signals, advice, claimable


# --- The unified quest queue (Command Center) ---------------------------------
# Display-merge of the two quest engines (command_intel.PilotDirective and
# readiness.PilotRecommendation). Both keep their own models, warmers, state and
# POST endpoints; this layer normalises them into ONE ranked list of dicts so the
# template renders one queue through one include and the engines cannot visually
# drift. A pilot sees six kinds of quest, not two systems.

# A digest row / quest action pointing into a disabled feature would 404 at the
# middleware — these maps let the view filter post-cache (caches stay
# feature-agnostic). /store/ and /freight/ are audience-gated member services
# (not Features), so they are always allowed.
KIND_FEATURE = {"operation": "operations", "srp": "srp", "task": "tasks",
                "task_open": "tasks", "train": "skills", "doctrine": "doctrines"}
URL_FEATURE = {"/skills/": "skills", "/operations/": "operations", "/tasks/": "tasks",
               "/doctrines/": "doctrines", "/srp/": "srp", "/industry/": "industry",
               "/stockpile/": "stockpile", "/killboard/": "killboard"}

_QUEST_ICONS = {
    "skill": "#i-rookie", "ship": "#i-box", "asset": "#i-ship",
    "logistics": "#i-truck", "industry": "#i-cube", "role": "#i-shield",
}


def action_feature_ok(url: str) -> bool:
    """False when the action URL's owning feature is disabled (avoids 404 links)."""
    from core.features import feature_enabled

    key = next((f for prefix, f in URL_FEATURE.items() if url.startswith(prefix)), None)
    return key is None or feature_enabled(key)


def _quest(obj, *, engine, category_key, rank, corp_order=False, now=None) -> dict:
    from django.utils import timezone

    now = now or timezone.now()
    return {
        "engine": engine,
        "id": obj.id,
        "category_key": category_key,
        "category_label": obj.get_category_display(),
        "icon": "#i-command" if corp_order else _QUEST_ICONS.get(category_key, "#i-target"),
        "corp_order": corp_order,
        "title": obj.title,
        "detail": obj.detail,
        "points": obj.points,
        "action_url": obj.action_url,
        "action_available": bool(obj.action_url) and action_feature_ok(obj.action_url),
        "form_url_name": ("command_intel:directive_action" if engine == "ci"
                          else "readiness:reco_action"),
        "is_new": (now - obj.created_at).total_seconds() < 86400,
        "rank": rank,
    }


def unified_quest_queue(directives, recos, career=()) -> list[dict]:
    """Merge the engines' open items into one ranked queue of quest dicts.

    Dedup contract (the merge's core promise — advice appears exactly once,
    and a duplicate class is dropped ONLY when CI actually carries it):
    readiness 'skill' recos are dropped when CI emitted its own train cards
    (both engines build them from the same ``closest_doctrines`` list — but a
    ship-only CI queue must NOT silence the pilot's training quests); the
    readiness 'fly a fleet' fallback is dropped when CI carries its own; a
    readiness ship reco whose hull already appears in a CI SHIP directive
    title is dropped (same buy-this-hull ask, differently justified).

    ``career`` is ``apps.capsuleer.briefing.career_quests(user)`` — at most one
    pre-normalized quest dict (doc 08 §10). With ``career=()`` the result is
    byte-identical to before, so the pinned merge tests do not change. A career
    row is suppressed when a surviving CI/readiness item already carries the same
    subject (doctrine or ship hull, by resolved name) — advice appears once; the
    goal page still shows the step.

    Ranking: constraint-grounded CI orders first (1000 + leverage — relieving
    the corp's binding constraint beats everything), then readiness recos by
    their priority (40–100), with CI fallback training (40 + points) in between.
    """
    from django.utils import timezone

    now = timezone.now()
    items: list[dict] = []
    ci_has_skill = any(d.category == "skill" for d in directives)
    ci_has_activity = any(d.slug == "stay-current/join-op" for d in directives)
    ci_ship_titles = " || ".join(d.title.lower() for d in directives if d.category == "ship")
    for d in directives:
        rank = 1000 + d.leverage if d.constraint_key else 40 + d.points
        items.append(_quest(d, engine="ci", category_key=d.category, rank=rank,
                            corp_order=bool(d.constraint_key), now=now))
    for r in recos:
        if ci_has_skill and r.category == "skill":
            continue  # duplicate of the CI train-into cards (same source list)
        if ci_has_activity and r.ref_type == "activity":
            continue  # 'Fly a fleet this week' fallback — CI carries its own
        if r.category == "ship":
            hull = r.title.lower().removeprefix("get your ").strip()
            if hull and hull in ci_ship_titles:
                continue  # a CI ship order already asks for this hull
        items.append(_quest(r, engine="readiness", category_key=r.category,
                            rank=r.priority, now=now))
    if career:
        surviving = " || ".join(q["title"].lower() for q in items)
        for c in career:
            if not _career_subject_collides(c, surviving):
                items.append(c)
    items.sort(key=lambda q: (q["rank"], q["points"]), reverse=True)
    return items


def _career_subject_collides(career_row, surviving_titles) -> bool:
    """True when a surviving quest already carries the career row's subject hull/doctrine (doc 08
    §10 collision-yield) — matched by resolved name, the house ``ci_ship_titles`` idiom."""
    ship_id = career_row.get("subject_ship_type_id")
    doctrine_id = career_row.get("subject_doctrine_id")
    if ship_id:
        from apps.sde.models import SdeType

        name = (SdeType.objects.filter(type_id=ship_id).values_list("name", flat=True).first() or "")
        if name and name.lower() in surviving_titles:
            return True
    if doctrine_id:
        from apps.doctrines.models import Doctrine

        name = (Doctrine.objects.filter(id=doctrine_id).values_list("name", flat=True).first() or "")
        if name and name.lower() in surviving_titles:
            return True
    return False


def leadership_briefing() -> dict:
    """Corp-level digest for officers — aggregates, not individuals.

    Cached corp-wide for a few minutes: it re-aggregates SRP exposure and stock
    shortfalls in Python, and it renders on every officer view of the pilots'
    highest-traffic page (including each post-directive-action redirect).
    """
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone

    cache_key = "briefing:leadership:v1"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from apps.killboard.models import Killmail
    from apps.readiness.services import compute_readiness
    from apps.srp.services import exposure
    from apps.stockpile.models import HaulingTask
    from apps.stockpile.services import shortfalls_against_targets
    from apps.tasks.models import Task

    from .services import corp_monthly_totals, points_leaderboard, recognition_feed

    readiness = compute_readiness()
    since = timezone.now() - timedelta(days=1)
    losses_24h = Killmail.objects.filter(
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM, killmail_time__gte=since
    ).count()

    result = {
        "index": readiness["index"],
        "top_gaps": readiness["gaps"][:5],
        "srp_exposure": exposure(),
        "stock_shortfalls": len(shortfalls_against_targets()),
        "open_tasks": Task.objects.filter(status=Task.Status.OPEN).count(),
        "open_hauls": HaulingTask.objects.filter(status=HaulingTask.Status.OPEN).count(),
        "losses_24h": losses_24h,
        # Corp contribution this month (native units) + who's been pitching in.
        "contrib_totals": corp_monthly_totals(),
        "recognition": recognition_feed(limit=8),
        "leaderboard": points_leaderboard(limit=10),
    }
    cache.set(cache_key, result, 300)
    return result
