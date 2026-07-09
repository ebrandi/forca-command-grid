"""EVE mail relay: keep broadcast mail, skip DMs, post only fresh, idempotent."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.recommendations import mail_relay as M
from apps.recommendations.models import RelayedMail


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def get(self, path, token=None):
        return _Resp(self._rows)


def _mail(mid, *, subject, when, rtype="corporation", sender=None):
    return {
        "mail_id": mid, "subject": subject, "from": sender,
        "timestamp": when, "recipients": [{"recipient_id": 1, "recipient_type": rtype}],
    }


@pytest.fixture
def _granted(monkeypatch):
    monkeypatch.setattr(M, "_token_character",
                        lambda corp_id: type("C", (), {"character_id": 99})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")


@pytest.mark.django_db
def test_relays_fresh_broadcasts_only(_granted, monkeypatch):
    fresh = timezone.now().isoformat()
    old = (timezone.now() - dt.timedelta(days=2)).isoformat()
    rows = [
        _mail(1, subject="Strat op tonight", when=fresh, rtype="corporation"),
        _mail(2, subject="Alliance ping", when=fresh, rtype="mailing_list"),
        _mail(3, subject="hi friend", when=fresh, rtype="character"),   # DM → skip
        _mail(4, subject="Old fleet", when=old, rtype="alliance"),       # stored, not posted
    ]
    posted = []
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg: posted.append(msg) or 1)

    res = M.sync_corp_mail(corp_id=1, client=_Client(rows))
    assert res["status"] == "ok"
    # Three broadcasts stored (1, 2, 4); the DM (3) is ignored entirely.
    assert set(RelayedMail.objects.values_list("mail_id", flat=True)) == {1, 2, 4}
    # Only the two fresh broadcasts were posted to Discord.
    assert len(posted) == 2 and any("Strat op tonight" in p for p in posted)

    # Idempotent: re-syncing the same set posts nothing new.
    posted.clear()
    res2 = M.sync_corp_mail(corp_id=1, client=_Client(rows))
    assert res2["new"] == 0 and posted == []


@pytest.mark.django_db
def test_no_token_is_noop(monkeypatch):
    monkeypatch.setattr(M, "_token_character", lambda corp_id: None)
    assert M.sync_corp_mail(corp_id=1)["status"] == "no_token"
    assert RelayedMail.objects.count() == 0
