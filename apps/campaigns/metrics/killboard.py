"""``killboard.kills`` — home-corp kills in the campaign window (doc 00 §6, doc 02 §4.16).

A windowed count of ``Killmail`` rows where the home corp was on the attacking side
(``involves_home_corp=True, home_corp_role=attacker``) — served by the ``km_home_role_time_idx``
composite index. The transparent explanation table (brief §4) and leadership-chosen weights are the
guard against an unhealthy raw kill-count metric.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from .base import Measurement, MetricSource, _dec, register


class KillboardKills(MetricSource):
    key = "killboard.kills"
    label = _("Killboard — home-corp kills in window")
    unit = "kills"
    data_class = "killmail"
    params_schema = []

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.killboard.models import Killmail

        now = params.get("_now") or timezone.now()
        since = params.get("_since")

        qs = Killmail.objects.filter(
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.ATTACKER,
            killmail_time__lte=now,
        )
        if since:
            qs = qs.filter(killmail_time__gte=since)
        count = qs.count()
        return Measurement(value=_dec(count), as_of=now,
                           detail={"since": since.isoformat() if since else None})


register(KillboardKills())
