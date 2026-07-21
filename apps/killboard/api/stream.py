"""KB-29 — the outbound realtime stream endpoint (``GET /api/killboard/stream/``).

One endpoint, two shapes over one cursor contract:

* default — a **bounded Server-Sent-Events** stream (``text/event-stream``): heartbeated,
  auto-closing after ``KILLBOARD_STREAM_MAX_LIFETIME_S``, resumable via ``Last-Event-ID`` or
  ``?after_seq=``, and capped by a Redis semaphore that returns **503 + Retry-After** when the
  worker budget is full;
* ``?mode=poll`` — a one-shot JSON batch of events after the cursor, the cheap fallback the
  live-feed page degrades to when the SSE cap is full or the feature is disabled.

Auth/permission/throttle are inherited from the KB-28 API (session or bearer token; anonymous
only under ``KILLBOARD_API_PUBLIC_READ``, and then only public topics — the member-gated
``deviated-losses`` / ``needs-srp`` topics 403 for non-members, and their flags are withheld
from the anonymous payload). See :mod:`apps.killboard.stream` for the worker-budget assessment.
"""
from __future__ import annotations

import json

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.killboard import stream
from core import rbac

from .permissions import IsMemberOrPublicRead
from .views import KillboardAPIViewMixin


class EventStreamRenderer(BaseRenderer):
    """Lets DRF content-negotiation accept an ``Accept: text/event-stream`` request (the
    browser ``EventSource`` default) instead of 406-ing before the handler runs. The SSE body
    itself is a raw ``StreamingHttpResponse`` — this renderer is used only if an *error*
    ``Response`` (e.g. a 503) is returned while that Accept is in force, so it just JSON-encodes."""

    media_type = "text/event-stream"
    format = "event-stream"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return json.dumps(data).encode(self.charset) if data is not None else b""


def _resume_cursor(request) -> int | None:
    """The client's resume point: ``Last-Event-ID`` (SSE reconnect) wins over ``?after_seq=``.

    ``None`` means "no cursor given" — the caller starts from the current tip so a fresh
    connection streams only genuinely new kills instead of replaying the whole ring buffer.
    """
    for raw in (request.headers.get("Last-Event-ID"), request.query_params.get("after_seq")):
        if raw not in (None, ""):
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                continue
    return None


@extend_schema(
    parameters=[
        OpenApiParameter("topics", str, description=(
            "Comma list, OR-combined. Public: all, kills, losses, secband:<band>, "
            "iskband:<floor>, shipclass:<class>, system:<id>, pilot:<character_id>. "
            "Member-only: deviated-losses, needs-srp. Default: all.")),
        OpenApiParameter("after_seq", int, description=(
            "Resume after this event seq (else Last-Event-ID, else the current tip).")),
        OpenApiParameter("mode", str, description="`poll` for a one-shot JSON batch; omitted for the SSE stream."),
    ],
    responses=OpenApiTypes.OBJECT,
)
class KillboardStreamView(KillboardAPIViewMixin, APIView):
    """The realtime home-corp killfeed (SSE, or ``?mode=poll`` JSON)."""

    permission_classes = [IsMemberOrPublicRead]
    # JSON first (poll mode + error bodies); the event-stream renderer only satisfies
    # content-negotiation for an `Accept: text/event-stream` SSE client.
    renderer_classes = [JSONRenderer, EventStreamRenderer]

    def get(self, request):
        if not stream._enabled():
            return self._unavailable("The realtime stream is disabled.")

        member = rbac.has_role(request.user, rbac.ROLE_MEMBER)
        try:
            matcher = stream.build_matcher(request.query_params.get("topics"), member=member)
        except stream.TopicError as exc:
            status = 403 if exc.forbidden else 400
            return Response({"detail": str(exc)}, status=status)

        cursor = _resume_cursor(request)
        if cursor is None:
            cursor = stream.tip_seq()  # fresh connection → only future events

        if (request.query_params.get("mode") or "").lower() == "poll":
            return Response(stream.poll_batch(cursor, matcher, member=member))

        # SSE: claim a semaphore slot up-front so a full pool is rejected before a thread is
        # tied up. The client falls back to ?mode=poll on this 503.
        slot = stream.acquire_slot()
        if slot is None:
            return self._unavailable("The realtime stream is at capacity.")
        return stream.stream_response(cursor, matcher, member=member, slot=slot)

    @staticmethod
    def _unavailable(detail: str) -> Response:
        resp = Response({"detail": detail}, status=503)
        resp["Retry-After"] = "30"
        return resp
