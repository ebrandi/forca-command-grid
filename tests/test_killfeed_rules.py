"""KB-24 — the kill-feed require/exclude rule engine."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.killboard.killfeed_rules import evaluate, ship_classes_for, staging_distances
from apps.killboard.models import KillFeedConfig, KillFeedPing, Killmail, KillmailParticipant

HOME = 98000001


def _cfg(**over):
    base = dict(
        min_loss_value=Decimal("100000000"), min_kill_value=Decimal("500000000"),
        exclude_npc=False, exclude_awox=False, require_solo=False,
        min_attackers=0, max_attackers=0, sec_bands=[], ship_classes=[],
        max_jumps_from_staging=0, losses_deviated_only=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _km(**over):
    base = dict(
        home_corp_role=Killmail.HomeRole.ATTACKER, total_value=Decimal("600000000"),
        is_npc=False, is_awox=False, is_solo=False, sec_band="nullsec",
        solar_system_id=30000142, fit_deviation=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ev(km, cfg, *, attacker_count=1, ship_class="Frigate", staging_distance=None):
    return evaluate(km, cfg, attacker_count=attacker_count, ship_class=ship_class,
                    staging_distance=staging_distance)


# --- evaluate: ISK floor + all-default parity ---------------------------------
def test_isk_floor_unchanged():
    assert _ev(_km(total_value=Decimal("600000000")), _cfg())
    assert not _ev(_km(total_value=Decimal("100000000")), _cfg())  # under kill bar


def test_all_default_rules_equal_threshold_only():
    # Every clause off -> only the ISK floor decides, even for npc/awox/blob/highsec kills.
    km = _km(is_npc=True, is_awox=True, sec_band="highsec")
    assert _ev(km, _cfg(), attacker_count=99, ship_class="Capital")


def test_zero_threshold_mutes_direction():
    assert not _ev(_km(home_corp_role=Killmail.HomeRole.ATTACKER), _cfg(min_kill_value=Decimal("0")))


# --- evaluate: each clause ----------------------------------------------------
def test_attacker_min_and_max():
    assert not _ev(_km(), _cfg(min_attackers=5), attacker_count=3)
    assert _ev(_km(), _cfg(min_attackers=5), attacker_count=5)
    assert not _ev(_km(), _cfg(max_attackers=2), attacker_count=3)
    assert _ev(_km(), _cfg(max_attackers=2), attacker_count=2)


def test_sec_band_include():
    assert _ev(_km(sec_band="lowsec"), _cfg(sec_bands=["lowsec"]))
    assert not _ev(_km(sec_band="highsec"), _cfg(sec_bands=["lowsec"]))


def test_ship_class_include():
    assert _ev(_km(), _cfg(ship_classes=["Battleship"]), ship_class="Battleship")
    assert not _ev(_km(), _cfg(ship_classes=["Battleship"]), ship_class="Frigate")


def test_exclude_npc_awox_and_require_solo():
    assert not _ev(_km(is_npc=True), _cfg(exclude_npc=True), attacker_count=0)
    assert not _ev(_km(is_awox=True), _cfg(exclude_awox=True))
    assert not _ev(_km(is_solo=False), _cfg(require_solo=True), attacker_count=3)
    assert _ev(_km(is_solo=True), _cfg(require_solo=True))


def test_jumps_from_staging():
    cfg = _cfg(max_jumps_from_staging=3)
    dist = {30000142: 0, 30000143: 2, 30000144: 5}
    assert _ev(_km(solar_system_id=30000143), cfg, staging_distance=dist)      # 2 <= 3
    assert not _ev(_km(solar_system_id=30000144), cfg, staging_distance=dist)  # 5 > 3
    assert not _ev(_km(solar_system_id=99999), cfg, staging_distance=dist)     # out of range
    assert not _ev(_km(), cfg, staging_distance=None)  # clause set but no staging -> blocked


def test_deviated_losses_only():
    cfg = _cfg(losses_deviated_only=True)
    loss = dict(home_corp_role=Killmail.HomeRole.VICTIM, total_value=Decimal("200000000"))
    # a kill is never a "deviated loss"
    assert not _ev(_km(home_corp_role=Killmail.HomeRole.ATTACKER), cfg)
    # a loss with a clean fit -> blocked
    assert not _ev(_km(**loss, fit_deviation=SimpleNamespace(is_clean=True)), cfg)
    # a loss that broke doctrine -> posts
    assert _ev(_km(**loss, fit_deviation=SimpleNamespace(is_clean=False)), cfg)


# --- ship_classes_for (DB) ----------------------------------------------------
@pytest.mark.django_db
def test_ship_classes_for(sde):
    out = ship_classes_for({587, 600})   # Rifter -> Frigate, Test Cruiser -> Cruiser
    assert out[587] == "Frigate"
    assert out[600] == "Cruiser"


# --- staging distances (DB) ---------------------------------------------------
@pytest.mark.django_db
def test_staging_distances(sde):
    from apps.navigation.highsec_exit import clear_gate_cache
    from apps.readiness.models import StagingSystem
    from apps.sde.models import SdeSystemJump

    for a, b in [(1, 2), (2, 3)]:   # a small pocket A-B-C (adjacency self-symmetrises)
        SdeSystemJump.objects.create(from_system_id=a, to_system_id=b)
    clear_gate_cache()
    StagingSystem.objects.create(system_id=1, system_name="A", active=True)

    assert staging_distances(_cfg(max_jumps_from_staging=0)) is None  # clause off
    dist = staging_distances(_cfg(max_jumps_from_staging=3))
    assert dist[1] == 0 and dist[2] == 1 and dist[3] == 2


# --- run_kill_feed end-to-end -------------------------------------------------
def _mail(km_id, *, ship=587, value="600000000", role=Killmail.HomeRole.ATTACKER):
    return Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=timezone.now() - timedelta(hours=1),
        solar_system_id=30000142, victim_ship_type_id=ship, involves_home_corp=True,
        home_corp_role=role, total_value=Decimal(value),
    )


@pytest.mark.django_db
def test_run_kill_feed_ship_class_rule(sde):
    from apps.killboard.killfeed import run_kill_feed

    cfg = KillFeedConfig.load()
    cfg.enabled = True
    cfg.min_kill_value = Decimal("100000000")
    cfg.ship_classes = ["Cruiser"]   # only cruiser victims
    cfg.save()

    _mail(1, ship=587)   # Frigate -> blocked
    _mail(2, ship=600)   # Cruiser -> posts

    posted = []
    res = run_kill_feed(client_post=lambda m: posted.append(m) or 1)
    assert res["posted"] == 1
    assert set(KillFeedPing.objects.values_list("killmail_id", flat=True)) == {2}


@pytest.mark.django_db
def test_run_kill_feed_attacker_count_rule(sde):
    from apps.killboard.killfeed import run_kill_feed

    cfg = KillFeedConfig.load()
    cfg.enabled = True
    cfg.min_kill_value = Decimal("100000000")
    cfg.max_attackers = 2   # small-gang only
    cfg.save()

    blob = _mail(1)
    for i in range(3):
        KillmailParticipant.objects.create(killmail=blob, role="attacker", seq=i + 1,
                                           character_id=1000 + i, corporation_id=HOME,
                                           ship_type_id=587, damage_done=10)
    small = _mail(2)
    KillmailParticipant.objects.create(killmail=small, role="attacker", seq=1,
                                       character_id=2000, corporation_id=HOME,
                                       ship_type_id=587, damage_done=10)

    run_kill_feed(client_post=lambda m: 1)
    assert set(KillFeedPing.objects.values_list("killmail_id", flat=True)) == {2}


@pytest.mark.django_db
def test_config_page_saves_and_sanitizes_rules(client, django_user_model, sde):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role

    officer = django_user_model.objects.create(username="off")
    RoleAssignment.objects.create(user=officer, role=ensure_role("officer"))
    client.force_login(officer)

    resp = client.post("/killboard/killfeed/settings/", {
        "section": "killfeed", "enabled": "1",
        "min_loss_value": "100000000", "min_kill_value": "500000000",
        "min_attackers": "5", "max_attackers": "0",
        "sec_bands": ["lowsec", "nullsec", "bogus"],       # bogus must be dropped
        "ship_classes": ["Cruiser", "Battleship", "Nope"],  # Nope must be dropped
        "max_jumps_from_staging": "4",
        "exclude_npc": "1", "require_solo": "1",
    })
    assert resp.status_code == 302

    cfg = KillFeedConfig.load()
    assert cfg.min_attackers == 5 and cfg.max_attackers == 0
    assert cfg.sec_bands == ["lowsec", "nullsec"]
    assert cfg.ship_classes == ["Cruiser", "Battleship"]
    assert cfg.max_jumps_from_staging == 4
    assert cfg.exclude_npc is True and cfg.require_solo is True
    assert cfg.exclude_awox is False and cfg.losses_deviated_only is False
