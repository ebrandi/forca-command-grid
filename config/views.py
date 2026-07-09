"""Project-level views: health check and public landing page."""
from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe used by Docker, nginx, and the deploy script."""
    db_ok = True
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:  # noqa: BLE001 - report unhealthy, never raise
        db_ok = False
    status = 200 if db_ok else 503
    return JsonResponse({"status": "ok" if db_ok else "degraded", "database": db_ok}, status=status)


def landing(request: HttpRequest) -> HttpResponse:
    """Public landing page (recruitment surface + login CTA)."""
    from apps.kb.models import KbPage
    from core.features import feature_enabled

    public_pages = []
    if feature_enabled("knowledge_base"):
        public_pages = list(
            KbPage.objects.filter(visibility=KbPage.Visibility.PUBLIC)
            .order_by("category", "title")[:12]
        )
    return render(request, "landing.html", {"kb_public_pages": public_pages})


def showcase(request: HttpRequest) -> HttpResponse:
    """Public "features" gallery — a visual tour of the platform (recruiting surface).

    Anonymous-accessible and indexable. Content lives in ``config.showcase_data``; here we
    just decorate it with a running index, alternating layout, and per-category counts.
    """
    import collections

    from config.showcase_data import CATEGORIES, CATEGORY_LABELS, FEATURES, PRIVATE_FEATURES

    counts = collections.Counter(f["cat"] for f in FEATURES)
    categories = [
        {"key": key, "label": label, "count": counts[key]}
        for key, label in CATEGORIES
        if counts.get(key)
    ]
    features = []
    for i, f in enumerate(FEATURES, start=1):
        features.append({
            **f,
            "idx": f"{i:02d}",
            "flip": i % 2 == 0,          # alternate screenshot/copy sides for rhythm
            "category_key": f["cat"],
            "category_label": CATEGORY_LABELS.get(f["cat"], f["cat"]),
            "thumbs": f.get("thumbs", []),
        })
    return render(request, "showcase.html", {
        # NB: NOT "features" — that key is the feature-flag map from the core.context.roles
        # context processor (used by the nav); a view context of the same name would shadow
        # it and break {% if features.x %} nav gating on this page.
        "showcase_features": features,
        "categories": categories,
        "system_count": len(categories),
        "private_features": PRIVATE_FEATURES,
    })
