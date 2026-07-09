"""Recipient resolution + the per-channel delivery fan-out.

Record-then-deliver: the ``Alert``/``AlertDelivery``/``AlertRecipient`` rows are
written before any provider is touched, each channel is isolated in its own
try/except (one broken provider never blocks the others), and a channel is marked
delivered only on a real send so a down provider stays retriable.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from . import config
from .models import (
    ALERT_TERMINAL,
    Alert,
    AlertDelivery,
    AlertRecipient,
    AlertStatus,
    ChannelProvider,
    DeliveryStatus,
)
from .providers import Recipient, provider_class

log = logging.getLogger("forca.pingboard")

# Delivery modes by channel kind:
#  - PER_USER_KINDS resolve to per-recipient rows sent through the provider (in-app users,
#    eve-mail character ids).
#  - DM_HANDLE_KINDS resolve to verified per-pilot PilotContactChannel handles (DMs).
#  - BROADCAST_ONLY_KINDS post to a configured channel/webhook (no per-user resolution).
PER_USER_KINDS = {"in_app", "eve_mail"}
DM_HANDLE_KINDS = {"slack", "telegram", "whatsapp"}
BROADCAST_ONLY_KINDS = {"discord"}

_HANDLE_RTYPE = {"slack": "slack_user", "telegram": "chat", "whatsapp": "phone"}


class RecipientResolver:
    """Turn an ``audience`` spec + channel kind into concrete recipients.

    A resolver is a single-dispatch object: it memoises each audience's user set so
    the several ``resolve()`` calls (one per channel kind) plus ``estimate()`` within
    one dispatch hit the roster query once, not once per channel.
    """

    def __init__(self) -> None:
        self._user_cache: dict[str, list] = {}

    def resolve(
        self, audience: dict | None, kind: str, category: str = "", priority: str = ""
    ) -> list[Recipient]:
        if kind in BROADCAST_ONLY_KINDS:
            return []  # provider posts to its configured destination
        users = self._audience_users(audience or {"kind": "corp"})
        if kind == "in_app":
            return [Recipient(kind, "user", str(u.id), u.id, self._name(u)) for u in users]
        if kind == "eve_mail":
            out: list[Recipient] = []
            for u in users:
                cid = self._main_char_id(u)
                if cid:
                    out.append(Recipient(kind, "character", str(cid), u.id, self._name(u)))
            return out
        if kind in DM_HANDLE_KINDS:
            if not users:
                return []
            from .models import PilotContactChannel

            user_ids = [u.id for u in users]
            muted = self._muted_user_ids(kind, category, priority, user_ids)
            rtype = _HANDLE_RTYPE.get(kind, "chat")
            rows = PilotContactChannel.objects.filter(
                kind=kind, verified=True, user_id__in=user_ids
            )
            return [
                Recipient(kind, rtype, r.handle, r.user_id)
                for r in rows
                if r.handle and r.user_id not in muted
            ]
        return []

    def estimate(self, audience: dict | None) -> int:
        """Resolved distinct-user count for the composer's recipient estimate."""
        return len(self._audience_users(audience or {"kind": "corp"}))

    # -- internals -------------------------------------------------------------
    def _muted_user_ids(self, kind: str, category: str, priority: str, user_ids: list) -> set:
        """User ids that muted ``category`` on this DM ``kind``.

        Two hard safety floors ignore every mute so a corp-survival ping always
        reaches a pilot's linked channels: the EMERGENCY *category*, and any alert at
        EMERGENCY *priority* (catching an emergency-priority alert filed under another
        category). A blank category (a legacy caller that did not thread one through)
        applies no mute, preserving prior behaviour.
        """
        from .models import AlertCategory, AlertPriority, PilotChannelPreference

        if (
            not category
            or category == AlertCategory.EMERGENCY
            or priority == AlertPriority.EMERGENCY
            or not user_ids
        ):
            return set()
        return set(
            PilotChannelPreference.objects.filter(
                kind=kind, category=category, muted=True, user_id__in=user_ids
            ).values_list("user_id", flat=True)
        )

    def _audience_users(self, audience: dict) -> list:
        import json

        key = json.dumps(audience or {"kind": "corp"}, sort_keys=True, default=str)
        cached = self._user_cache.get(key)
        if cached is not None:
            return cached
        users = self._resolve_audience_users(audience)
        self._user_cache[key] = users
        return users

    def _resolve_audience_users(self, audience: dict) -> list:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        kind = (audience or {}).get("kind", "corp")

        if kind == "corp":
            return list(User.objects.filter(characters__is_corp_member=True).distinct())
        if kind == "users":
            ids = audience.get("ids") or []
            return list(User.objects.filter(id__in=ids))
        if kind == "user":
            uid = audience.get("id")
            ids = audience.get("ids") or ([uid] if uid else [])
            return list(User.objects.filter(id__in=ids))
        if kind in ("role", "officer", "director", "admin", "member"):
            from core import rbac

            # ``{"kind":"role","role":...}`` or the shorthand ``{"kind":"officer"}`` the
            # category routing defaults emit — both resolve to every corp member at or
            # above the role, so an officer-audience alert actually reaches officers
            # in-app / by EVE-mail (not only via a broadcast channel).
            role = audience.get("role", rbac.ROLE_OFFICER) if kind == "role" else kind
            members = User.objects.filter(characters__is_corp_member=True).distinct()
            return [u for u in members if rbac.has_role(u, role)]
        if kind == "channel":
            return []  # a specific provider destination — no per-user resolution
        return []

    def _main_char_id(self, user):
        chars = list(user.characters.all())
        if not chars:
            return None
        for c in chars:
            if getattr(c, "is_main", False):
                return c.character_id
        return chars[0].character_id

    def _name(self, user) -> str:
        return getattr(user, "display_name", "") or user.get_username()


class AlertDispatcher:
    def dispatch(self, alert_id: int) -> dict:
        alert = Alert.objects.filter(pk=alert_id).first()
        if alert is None:
            return {"status": "missing"}
        if alert.status in ALERT_TERMINAL:
            return {"status": alert.status, "noop": True}

        now = timezone.now()
        if alert.expires_at and alert.expires_at <= now:
            alert.status = AlertStatus.EXPIRED
            alert.save(update_fields=["status", "updated_at"])
            return {"status": AlertStatus.EXPIRED}

        gen = config.get("general")
        if not gen["enabled"]:
            return {"status": "disabled"}

        alert.status = AlertStatus.SENDING
        alert.save(update_fields=["status", "updated_at"])

        channels = alert.channels or gen["default_channels"]
        resolver = RecipientResolver()
        any_ok = False
        any_fail = False

        # The classification this alert's audience implies. A restricted audience
        # (officers/directors/a named-pilot DM) may not be posted to a shared "mass"
        # chat destination whose ceiling doesn't clear it — the same guarantee the
        # broadcast_text path enforces, applied here so audience-restricted alerts never
        # leak onto a corp-wide channel while still reaching their pilots per-recipient.
        from .services import _classification_ok, audience_classification

        msg_classification = audience_classification(alert.audience)

        for kind in channels:
            recipients = resolver.resolve(alert.audience, kind, alert.category, alert.priority)
            self._record_recipients(alert, kind, recipients)
            rows = self._provider_rows(kind)
            # DM channels also get a global-token delivery (provider=None) for the resolved
            # per-pilot handles, on top of any configured broadcast channel/group rows.
            if kind in DM_HANDLE_KINDS and recipients:
                rows = list(rows) + [None]
            if not rows:
                self._delivery(alert, kind, None, status=DeliveryStatus.SKIPPED,
                               error="no enabled provider for this channel")
                continue
            pcls = provider_class(kind)
            for row in rows:
                # A shared/mass destination is any broadcast-only channel (Discord webhook)
                # or a configured group/channel row for a DM kind (a Slack channel, a
                # Telegram group). The per-recipient legs (in-app, EVE-mail, the DM-handle
                # leg where row is None) address exactly the resolved pilots and are exempt.
                is_shared_destination = kind in BROADCAST_ONLY_KINDS or (
                    kind in DM_HANDLE_KINDS and row is not None
                )
                if is_shared_destination and not _classification_ok(
                    getattr(row, "max_classification", ""), msg_classification
                ):
                    self._delivery(
                        alert, kind, row, status=DeliveryStatus.SKIPPED,
                        error="restricted audience: channel classification ceiling too low",
                    )
                    continue
                existing = AlertDelivery.objects.filter(alert=alert, kind=kind, provider=row).first()
                # Deliver-once across retries: never re-send a delivered channel.
                if existing and existing.status == DeliveryStatus.DELIVERED:
                    any_ok = True
                    continue
                if existing and existing.attempts >= existing.max_attempts:
                    any_fail = True
                    continue
                if pcls is None:
                    self._delivery(alert, kind, row, status=DeliveryStatus.SKIPPED,
                                   error="provider not implemented yet")
                    continue
                try:
                    result = pcls(row).send(
                        subject=alert.title, body=alert.body,
                        recipients=self._send_recipients(kind, row, recipients),
                    )
                except Exception:  # noqa: BLE001 - a provider must never crash the dispatcher
                    log.exception("Pingboard provider %s crashed", kind)
                    self._delivery(alert, kind, row, status=DeliveryStatus.FAILED,
                                   error="provider raised", bump_attempt=True)
                    any_fail = True
                    continue
                self._delivery(alert, kind, row, from_result=result)
                if row is not None:
                    self._update_health(row, result)
                if result.ok:
                    any_ok = True
                elif not result.skipped:
                    any_fail = True

        alert.recipient_count = resolver.estimate(alert.audience)
        alert.sent_at = timezone.now()
        alert.status = (
            AlertStatus.SENT if (any_ok and not any_fail)
            else AlertStatus.PARTIAL if any_ok
            else AlertStatus.FAILED
        )
        alert.save(update_fields=["recipient_count", "sent_at", "status", "updated_at"])
        return {"status": alert.status, "recipients": alert.recipient_count}

    # -- helpers ---------------------------------------------------------------
    def _provider_rows(self, kind: str):
        if kind == "in_app":
            return [None]
        return list(ChannelProvider.objects.filter(kind=kind, enabled=True))

    def _send_recipients(self, kind, row, recipients):
        """Recipients go to the provider only for per-user sends (in-app/eve-mail) and for
        the global-token DM delivery (row is None); a broadcast channel row gets none."""
        if kind in PER_USER_KINDS:
            return recipients
        if kind in DM_HANDLE_KINDS and row is None:
            return recipients
        return []

    def _record_recipients(self, alert: Alert, kind: str, recipients: list[Recipient]) -> None:
        if not recipients:
            return
        if AlertRecipient.objects.filter(alert=alert, kind=kind).exists():
            return  # already recorded (retry) — do not duplicate
        AlertRecipient.objects.bulk_create([
            AlertRecipient(alert=alert, kind=kind, recipient_type=r.recipient_type,
                           recipient_ref=r.recipient_ref, user_id=r.user_id)
            for r in recipients
        ])

    def _delivery(self, alert, kind, provider, *, status=None, error="",
                  from_result=None, bump_attempt=False):
        d, _ = AlertDelivery.objects.get_or_create(alert=alert, kind=kind, provider=provider)
        if from_result is not None:
            d.attempts += 1
            d.recipients_ok = from_result.recipients_ok
            d.recipients_failed = from_result.recipients_failed
            d.provider_message_id = (from_result.provider_message_id or "")[:128]
            d.last_error = (from_result.error or "")[:300]
            if from_result.ok:
                d.status = DeliveryStatus.DELIVERED
                d.delivered_at = timezone.now()
            elif from_result.skipped:
                d.status = DeliveryStatus.SKIPPED
            else:
                d.status = DeliveryStatus.FAILED
        else:
            if bump_attempt:
                d.attempts += 1
            d.status = status or d.status
            d.last_error = (error or "")[:300]
        d.save()
        return d

    def _update_health(self, row: ChannelProvider, result) -> None:
        now = timezone.now()
        if result.ok:
            row.last_ok_at = now
            row.last_error = ""
            row.save(update_fields=["last_ok_at", "last_error", "updated_at"])
        elif not result.skipped:
            row.last_error = (result.error or "")[:300]
            row.last_error_at = now
            row.save(update_fields=["last_error", "last_error_at", "updated_at"])
