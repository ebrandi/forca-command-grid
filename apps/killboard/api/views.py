"""Views for the killboard REST API (KB-28). Read-only v1.

Every endpoint reuses the board's own logic — the same filter semantics as the public feed
(``killboard.views.killboard_list``), the same fit renderer, leaderboards and analytics — so
the API and the HTML board can never disagree. Access is RBAC-tiered; field-level tiering
(own-loss vs any-loss deviation/SRP) is applied in the serializer from context set here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework import viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.killboard import fitrender
from apps.killboard.models import Killmail, KillmailParticipant
from core import rbac

from .auth import KillboardTokenAuthentication
from .permissions import IsMember, IsMemberOrPublicRead
from .serializers import (
    KillmailDetailSerializer,
    KillmailListSerializer,
    fit_payload,
)
from .throttling import KillboardAnonThrottle, KillboardUserThrottle

_ATTACKER = KillmailParticipant.Role.ATTACKER
_VALID_SIDES = ("victim", "attacker")
# Entity filters: query param -> (victim column, attacker-participant column). Mirrors
# killboard.views._ENTITY_FILTERS so the API and the feed resolve the same rows.
_ENTITY_FILTERS = {
    "character_id": ("victim_character_id", "character_id"),
    "corporation_id": ("victim_corporation_id", "corporation_id"),
    "alliance_id": ("victim_alliance_id", "alliance_id"),
}
_TRUTHY = {"1", "true", "yes", "on"}


def _home() -> int:
    return getattr(settings, "FORCA_HOME_CORP_ID", 0)


class KillboardAPIViewMixin:
    """Shared auth + throttle wiring for every killboard API view.

    Session auth serves the logged-in browser member; the bearer token serves off-site
    integrations. The two throttles apply to anonymous (IP) and authenticated (user id)
    traffic respectively.
    """

    authentication_classes = [SessionAuthentication, KillboardTokenAuthentication]
    throttle_classes = [KillboardAnonThrottle, KillboardUserThrottle]


class KillmailCursorPagination(CursorPagination):
    """Stable id-DESC cursor paging — no COUNT, no drift as new kills land at the head."""

    ordering = "-killmail_id"
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


_LIST_FILTER_PARAMS = [
    OpenApiParameter("kind", str, description="`kills` or `losses` (home-corp role)."),
    OpenApiParameter("window", str, description="`7d`, `30d`, `90d` — or use `from`/`to`."),
    OpenApiParameter("from", OpenApiTypes.DATE, description="ISO date lower bound (inclusive)."),
    OpenApiParameter("to", OpenApiTypes.DATE, description="ISO date upper bound (exclusive)."),
    OpenApiParameter("sec_band", str, description="highsec/lowsec/nullsec/wh/abyssal/pochven/unknown."),
    OpenApiParameter("ship_type_id", int, description="Victim hull type id."),
    OpenApiParameter("ship_class", str, description="Broad hull class (Frigate…Capital, Other)."),
    OpenApiParameter("system_id", int),
    OpenApiParameter("region_id", int),
    OpenApiParameter("character_id", int, description="Victim, or attacker when `side=attacker`."),
    OpenApiParameter("corporation_id", int),
    OpenApiParameter("alliance_id", int),
    OpenApiParameter("side", str, description="`victim` (default) or `attacker` for the entity filters."),
    OpenApiParameter("solo", bool),
    OpenApiParameter("doctrine", str, description="A doctrine id, or `tagged` for any doctrine-tagged loss."),
    OpenApiParameter("min_value", OpenApiTypes.NUMBER, description="Minimum total ISK value."),
]


@extend_schema(parameters=_LIST_FILTER_PARAMS)
class KillmailViewSet(KillboardAPIViewMixin, viewsets.ReadOnlyModelViewSet):
    """The corp killfeed: list (filtered, cursor-paged) + detail, with fit exports.

    In the public-board-equivalent tier: readable anonymously when
    ``KILLBOARD_API_PUBLIC_READ`` is on, members always. Deviation and SRP fields on the
    detail are gated to the loss owner and officers.
    """

    permission_classes = [IsMemberOrPublicRead]
    pagination_class = KillmailCursorPagination
    lookup_field = "killmail_id"
    lookup_url_kwarg = "killmail_id"

    def get_serializer_class(self):
        return KillmailDetailSerializer if self.action == "retrieve" else KillmailListSerializer

    # --- queryset + filters (mirror killboard.views.killboard_list) ---------
    def get_queryset(self):
        qs = Killmail.objects.filter(involves_home_corp=True).select_related("doctrine_fit__doctrine")
        p = self.request.query_params

        kind = p.get("kind")
        if kind == "losses":
            qs = qs.filter(home_corp_role=Killmail.HomeRole.VICTIM)
        elif kind == "kills":
            qs = qs.filter(home_corp_role=Killmail.HomeRole.ATTACKER)

        qs = self._apply_window(qs, p)

        for param, col in (("system_id", "solar_system_id"), ("region_id", "region_id"),
                           ("ship_type_id", "victim_ship_type_id")):
            v = p.get(param)
            if v and v.isdigit():
                qs = qs.filter(**{col: int(v)})

        ship_class = p.get("ship_class")
        if ship_class:
            qs = self._apply_ship_class(qs, ship_class)

        band = p.get("sec_band")
        if band:
            qs = qs.filter(sec_band=band)

        side = p.get("side")
        side = side if side in _VALID_SIDES else "victim"
        for param, (vcol, pcol) in _ENTITY_FILTERS.items():
            v = p.get(param)
            if v and v.isdigit():
                ival = int(v)
                if side == "attacker":
                    sub = KillmailParticipant.objects.filter(
                        role=_ATTACKER, **{pcol: ival}
                    ).values("killmail_id")
                    qs = qs.filter(killmail_id__in=sub)
                else:
                    qs = qs.filter(**{vcol: ival})

        if (p.get("solo") or "").lower() in _TRUTHY:
            qs = qs.filter(is_solo=True)

        doctrine = p.get("doctrine")
        if doctrine:
            if doctrine.isdigit():
                qs = qs.filter(doctrine_fit__doctrine_id=int(doctrine))
            elif doctrine.lower() in _TRUTHY | {"tagged"}:
                qs = qs.filter(doctrine_fit__isnull=False)

        mv = p.get("min_value")
        if mv:
            try:
                qs = qs.filter(total_value__gte=Decimal(mv))
            except (InvalidOperation, ValueError):
                pass

        # Per-page enrichment as correlated subquery + a single prefetch — bounded regardless
        # of page size (no COUNT-over-GROUP-BY across the participant table; see the board).
        attacker_count = Coalesce(
            Subquery(
                KillmailParticipant.objects.filter(
                    killmail_id=OuterRef("killmail_id"), role=_ATTACKER
                ).order_by().values("killmail_id").annotate(c=Count("*")).values("c")[:1],
                output_field=IntegerField(),
            ),
            0,
        )
        return qs.annotate(attacker_count=attacker_count).prefetch_related(
            Prefetch(
                "participants",
                queryset=KillmailParticipant.objects.filter(role=_ATTACKER, final_blow=True),
                to_attr="final_blowers",
            )
        )

    def _apply_window(self, qs, p):
        window = p.get("window")
        days = {"7d": 7, "30d": 30, "90d": 90}.get(window)
        if days:
            qs = qs.filter(killmail_time__gte=timezone.now() - timedelta(days=days))
        frm, to = p.get("from"), p.get("to")
        if frm:
            d = _parse_date(frm)
            if d:
                qs = qs.filter(killmail_time__gte=d)
        if to:
            d = _parse_date(to)
            if d:
                qs = qs.filter(killmail_time__lt=d + timedelta(days=1))
        return qs

    def _apply_ship_class(self, qs, hull_class):
        from apps.doctrines.hulls import ALL_CLASSIFIED_GROUPS, group_ids_for_class
        from apps.sde.models import SdeType

        if hull_class == "Other":
            return qs.filter(
                victim_ship_type_id__in=SdeType.objects.exclude(
                    group_id__in=ALL_CLASSIFIED_GROUPS
                ).values("type_id")
            )
        groups = group_ids_for_class(hull_class)
        if not groups:
            return qs.none()
        return qs.filter(
            victim_ship_type_id__in=SdeType.objects.filter(group_id__in=groups).values("type_id")
        )

    # --- detail (tier-gated fields via context) -----------------------------
    def retrieve(self, request, *args, **kwargs):
        km = self.get_object()
        ctx = self.get_serializer_context()
        ctx["home_corp_id"] = _home()
        ctx["deviation"] = self._gated_deviation(request, km)
        ctx["srp"] = self._gated_srp(request, km)
        return Response(KillmailDetailSerializer(km, context=ctx).data)

    # --- fit exports (reuse the existing renderers/exporter) ----------------
    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["get"], url_path="fitting")
    def fitting(self, request, killmail_id=None):
        """Slot-bucketed fit JSON (``fitrender.build_fit``). Off-doctrine markers gated."""
        km = self.get_object()
        return Response(fit_payload(km, self._gated_deviation(request, km)))

    @extend_schema(responses=OpenApiTypes.STR)
    @action(detail=True, methods=["get"], url_path="eft")
    def eft(self, request, killmail_id=None):
        """The loss's fit as EFT text (copy into the game / Pyfa)."""
        from apps.doctrines.killmail_import import eft_from_killmail

        km = self.get_object()
        return HttpResponse(eft_from_killmail(km), content_type="text/plain; charset=utf-8")

    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["get"], url_path="esi")
    def esi(self, request, killmail_id=None):
        """The loss's fit as an ESI-shaped fitting dict (round-trips through pilot tooling)."""
        km = self.get_object()
        return Response(fitrender.esi_fitting(km))

    # --- ownership / tier gating -------------------------------------------
    def _viewer_is_owner(self, request, km) -> bool:
        user = request.user
        return bool(
            getattr(user, "is_authenticated", False)
            and km.victim_character_id
            and user.characters.filter(character_id=km.victim_character_id).exists()
        )

    def _can_see_private(self, request, km) -> bool:
        return self._viewer_is_owner(request, km) or rbac.has_role(request.user, rbac.ROLE_OFFICER)

    def _gated_deviation(self, request, km):
        if not self._can_see_private(request, km):
            return None
        deviation = getattr(km, "fit_deviation", None)
        if deviation is None or deviation.is_clean:
            return None
        return deviation

    def _gated_srp(self, request, km):
        if not self._can_see_private(request, km):
            return None
        return km.srp_claims.first()


# --------------------------------------------------------------------------- #
#  Stats / leaderboards (members only — mirrors the member-gated dashboard)
# --------------------------------------------------------------------------- #
class CorpStatsView(KillboardAPIViewMixin, APIView):
    """All-time corp headline (kills/losses/ISK/efficiency/danger)."""

    permission_classes = [IsMember]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from apps.killboard.analytics import summary

        return Response(summary())


class PilotStatsView(KillboardAPIViewMixin, APIView):
    """Per-pilot combat analytics — home-corp pilots only."""

    permission_classes = [IsMember]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, character_id: int):
        from apps.killboard.analytics import pilot_analytics
        from apps.sso.models import EveCharacter

        if not EveCharacter.objects.filter(
            character_id=character_id, is_corp_member=True
        ).exists():
            raise Http404("Not a home-corp pilot.")
        return Response(pilot_analytics(character_id))


class LeaderboardsView(KillboardAPIViewMixin, APIView):
    """The eight ranked boards for a window, optionally rolled up by main (KB-23)."""

    permission_classes = [IsMember]

    @extend_schema(
        parameters=[
            OpenApiParameter("window", str, description="7d/30d/90d/month/lastmonth/all (default month)."),
            OpenApiParameter("by_main", bool, description="Roll a person's alts up under their main."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        from apps.killboard.leaderboards import WINDOW_KEYS, leaderboards

        window = request.query_params.get("window", "month")
        if window not in WINDOW_KEYS:
            window = "month"
        p = request.query_params
        by_main = (p.get("by_main") or "").lower() in _TRUTHY or p.get("by") == "main"
        return Response(leaderboards(window, by_main=by_main))


# --------------------------------------------------------------------------- #
#  History (id+hash for gap-free re-ingest) — public-board-equivalent tier
# --------------------------------------------------------------------------- #
_HISTORY_MAX_N = 10000


class HistoryLatestView(KillboardAPIViewMixin, APIView):
    """The last-N ``(killmail_id, hash)`` for our corp, newest first (re-ingest cursor)."""

    permission_classes = [IsMemberOrPublicRead]

    @extend_schema(
        parameters=[OpenApiParameter("n", int, description=f"How many (1..{_HISTORY_MAX_N}, default 1000).")],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        raw = request.query_params.get("n", "1000")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 1000
        n = max(1, min(n, _HISTORY_MAX_N))
        rows = list(
            Killmail.objects.filter(involves_home_corp=True)
            .order_by("-killmail_id")
            .values("killmail_id", "killmail_hash")[:n]
        )
        return Response({
            "count": len(rows),
            "killmails": [{"killmail_id": r["killmail_id"], "hash": r["killmail_hash"]} for r in rows],
        })


class HistoryDayView(KillboardAPIViewMixin, APIView):
    """Every ``(killmail_id, hash)`` for our corp on one UTC day (``YYYYMMDD``)."""

    permission_classes = [IsMemberOrPublicRead]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, date: str):
        day = _parse_yyyymmdd(date)
        if day is None:
            raise Http404("Date must be YYYYMMDD.")
        start = timezone.make_aware(datetime(day.year, day.month, day.day))
        rows = list(
            Killmail.objects.filter(
                involves_home_corp=True, killmail_time__gte=start, killmail_time__lt=start + timedelta(days=1)
            )
            .order_by("killmail_id")
            .values("killmail_id", "killmail_hash")
        )
        return Response({
            "date": day.isoformat(),
            "count": len(rows),
            "killmails": [{"killmail_id": r["killmail_id"], "hash": r["killmail_hash"]} for r in rows],
        })


# --------------------------------------------------------------------------- #
#  Date parsing helpers
# --------------------------------------------------------------------------- #
def _parse_date(value: str):
    """A calendar date (from ``?from=/?to=``) as an aware midnight datetime, or None."""
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(datetime(d.year, d.month, d.day))


def _parse_yyyymmdd(value: str):
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  OpenAPI schema + docs UI (member-gated unless public-read is on)
# --------------------------------------------------------------------------- #
class _BrowserLoginRedirectMixin:
    """Bounce an anonymous HUMAN to SSO login instead of a bare 403 body.

    DRF enforces permissions in ``dispatch()`` before the handler runs, so the hook is
    ``handle_exception``: an auth/permission failure on a navigation request that prefers
    HTML becomes a login redirect with ``next`` back to the page. Programmatic clients
    (JSON/token) keep DRF's normal 401/403. Without this, an anonymous member clicking
    the docs link saw a blank page (a 13-byte 403 body).
    """

    def handle_exception(self, exc):
        from django.contrib.auth.views import redirect_to_login
        from rest_framework.exceptions import NotAuthenticated, PermissionDenied

        request = self.request
        if (isinstance(exc, NotAuthenticated | PermissionDenied)
                and not request.user.is_authenticated
                and "text/html" in request.headers.get("Accept", "")):
            return redirect_to_login(request.get_full_path())
        return super().handle_exception(exc)


class KillboardSchemaView(_BrowserLoginRedirectMixin, KillboardAPIViewMixin,
                          SpectacularAPIView):
    permission_classes = [IsMemberOrPublicRead]


class KillboardDocsView(_BrowserLoginRedirectMixin, KillboardAPIViewMixin,
                        SpectacularSwaggerView):
    permission_classes = [IsMemberOrPublicRead]
