"""Configurable Discord kill feed: thresholds, freshness, dedup, settings page."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard.models import KillFeedConfig, KillFeedPing, Killmail
from apps.sso.services import ensure_role
from core import rbac


def _km(km_id, *, role, value, ago_hours=1):
    return Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}",
        killmail_time=timezone.now() - timedelta(hours=ago_hours),
        solar_system_id=30000142, victim_ship_type_id=587,
        involves_home_corp=True, home_corp_role=role, total_value=Decimal(value),
    )


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.mark.django_db
def test_posts_only_qualifying_fresh_killmails(sde):
    from apps.killboard.killfeed import run_kill_feed

    cfg = KillFeedConfig.load()
    cfg.enabled = True
    cfg.min_loss_value = Decimal("100000000")   # 100M
    cfg.min_kill_value = Decimal("500000000")   # 500M
    cfg.save()

    _km(1, role=Killmail.HomeRole.VICTIM, value="200000000")   # loss over bar → post
    _km(2, role=Killmail.HomeRole.VICTIM, value="50000000")    # loss under bar → skip
    _km(3, role=Killmail.HomeRole.ATTACKER, value="600000000")  # kill over bar → post
    _km(4, role=Killmail.HomeRole.ATTACKER, value="600000000", ago_hours=48)  # too old → skip

    posted = []
    res = run_kill_feed(client_post=lambda msg: posted.append(msg) or 1)
    assert res["posted"] == 2
    assert any("Loss" in p for p in posted) and any("Kill" in p for p in posted)
    assert set(KillFeedPing.objects.values_list("killmail_id", flat=True)) == {1, 3}

    # Idempotent: a second run posts nothing (already pinged).
    posted.clear()
    assert run_kill_feed(client_post=lambda msg: posted.append(msg))["posted"] == 0
    assert posted == []


@pytest.mark.django_db
def test_disabled_is_noop(sde):
    from apps.killboard.killfeed import run_kill_feed

    _km(1, role=Killmail.HomeRole.VICTIM, value="999000000")
    assert run_kill_feed(client_post=lambda m: None)["status"] == "disabled"
    assert KillFeedPing.objects.count() == 0


@pytest.mark.django_db
def test_zero_threshold_mutes_that_direction(sde):
    from apps.killboard.killfeed import run_kill_feed

    cfg = KillFeedConfig.load()
    cfg.enabled = True
    cfg.min_loss_value = Decimal("0")           # losses muted
    cfg.min_kill_value = Decimal("100000000")
    cfg.save()
    _km(1, role=Killmail.HomeRole.VICTIM, value="999000000")   # muted
    _km(2, role=Killmail.HomeRole.ATTACKER, value="200000000")  # posted
    posted = []
    run_kill_feed(client_post=lambda m: posted.append(m))
    assert len(posted) == 1 and "Kill" in posted[0]


@pytest.mark.django_db
def test_settings_page_is_officer_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/killboard/killfeed/settings/").status_code == 403
    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    assert client.post("/killboard/killfeed/settings/",
                       {"enabled": "1", "min_loss_value": "250000000", "min_kill_value": "0"}).status_code == 302
    cfg = KillFeedConfig.load()
    assert cfg.enabled and cfg.min_loss_value == Decimal("250000000") and cfg.min_kill_value == Decimal("0")
