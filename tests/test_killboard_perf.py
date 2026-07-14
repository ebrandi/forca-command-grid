"""Tests for the killboard performance work (research/09 Part A) and the
configurable EVE-image origin (Part B).

Covers: the CombatMetric rollup read path with live fallback, the cache warmer,
the killfeed page-cache shim, the perf indexes being declared, and that imagery
URLs + the CSP img-src follow EVE_IMAGE_BASE_URL.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.killboard.models import CombatMetric, Killmail


def _home_corp():
    from django.conf import settings

    return settings.FORCA_HOME_CORP_ID


def _mk_killmail(**kw):
    defaults = dict(
        killmail_id=kw.pop("killmail_id", 1),
        killmail_hash="h",
        killmail_time=timezone.now(),
        solar_system_id=30000142,
        victim_ship_type_id=587,
        involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.ATTACKER,
        total_value=Decimal("1000"),
    )
    defaults.update(kw)
    return Killmail.objects.create(**defaults)


# --- A.1 CombatMetric rollup read path --------------------------------------
@pytest.mark.django_db
def test_summary_reads_corp_rollup_when_present():
    from apps.killboard.analytics import summary

    # A rollup row with sentinel values the live aggregation (no killmails) can't
    # produce — proves summary() served from the rollup, not a live scan.
    CombatMetric.objects.create(
        entity_type=CombatMetric.EntityType.CORPORATION, entity_id=_home_corp(),
        window="all", kills=42, losses=8, solo_kills=5,
        isk_destroyed=Decimal("900"), isk_lost=Decimal("100"),
    )
    s = summary()
    assert s["kills"] == 42 and s["losses"] == 8 and s["solo_kills"] == 5
    assert round(s["efficiency"]) == 90  # 900 / (900+100)


@pytest.mark.django_db
def test_summary_falls_back_to_live_without_rollup():
    from apps.killboard.analytics import summary

    _mk_killmail(killmail_id=10, is_npc=False, home_corp_role=Killmail.HomeRole.ATTACKER)
    s = summary()  # no CombatMetric row → live aggregation
    assert s["kills"] == 1 and s["losses"] == 0


@pytest.mark.django_db
def test_pilot_card_reads_rollup_with_final_blows():
    from apps.killboard.leaderboards import pilot_combat_card

    CombatMetric.objects.create(
        entity_type=CombatMetric.EntityType.CHARACTER, entity_id=99, window="all",
        kills=7, losses=2, solo_kills=3, final_blows=4, points=11,
        isk_destroyed=Decimal("500"), isk_lost=Decimal("500"),
    )
    card = pilot_combat_card(99, use_cache=False)
    assert card["has_record"] and card["kills"] == 7 and card["final_blows"] == 4
    assert card["points"] == 11 and round(card["efficiency"]) == 50


@pytest.mark.django_db
def test_pilot_card_live_fallback_without_rollup():
    from apps.killboard.leaderboards import pilot_combat_card

    card = pilot_combat_card(123456, use_cache=False)  # unknown pilot, no rollup
    assert card["has_record"] is False and card["kills"] == 0


@pytest.mark.django_db
def test_rebuild_member_metrics_stores_final_blows():
    from apps.killboard.models import KillmailParticipant
    from apps.killboard.stats import rebuild_member_metrics

    km = _mk_killmail(killmail_id=20, is_npc=False, home_corp_role=Killmail.HomeRole.ATTACKER)
    KillmailParticipant.objects.create(
        killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=0,
        character_id=777, corporation_id=_home_corp(), final_blow=True,
    )
    rebuild_member_metrics()
    row = CombatMetric.objects.get(
        entity_type=CombatMetric.EntityType.CHARACTER, entity_id=777, window="all"
    )
    assert row.kills == 1 and row.final_blows == 1


# --- A.4 Cache warmer --------------------------------------------------------
@pytest.mark.django_db
def test_warm_caches_populates_hot_keys():
    """These payloads embed translated prose, so their keys are language-scoped
    (``i18n_cache_key``) — the warmer fills one entry per enabled locale."""
    from apps.killboard.analytics import CACHE_VERSION, warm_caches, warm_languages

    cache.clear()
    warm_caches()
    home = _home_corp()
    for lang in warm_languages():
        assert cache.get(f"kb:stats:{CACHE_VERSION}:{home}:{lang}") is not None
        assert cache.get(f"kb:feed:{CACHE_VERSION}:{home}:{lang}") is not None
        assert cache.get(f"kb:lb:{CACHE_VERSION}:{home}:7d:{lang}") is not None
    # The officer loss-impact board carries no prose — it keeps a single, unscoped key.
    assert cache.get(f"kb:lossimpact:{CACHE_VERSION}:{home}:90") is not None


@pytest.mark.django_db
def test_warm_caches_fills_every_enabled_locale(settings):
    """The warmer runs under ``translation.override`` per locale: without that it would
    only ever fill ``:en`` and every other locale would take the cold-path recompute."""
    from apps.killboard.analytics import CACHE_VERSION, warm_caches
    from core.i18n.config import I18N_SETTING_KEY, set_i18n_config

    cache.clear()
    set_i18n_config(locales={"de": True})
    try:
        warm_caches()
        home = _home_corp()
        for lang in ("en", "de"):
            assert cache.get(f"kb:stats:{CACHE_VERSION}:{home}:{lang}") is not None
        # A locale nobody can be served in is not warmed (it would burn a full recompute).
        assert cache.get(f"kb:stats:{CACHE_VERSION}:{home}:ja") is None
    finally:
        from apps.admin_audit.models import AppSetting

        AppSetting.objects.filter(key=I18N_SETTING_KEY).delete()
        cache.clear()


# --- A.2 Killfeed page-cache shim -------------------------------------------
def test_count_paginator_shim_pagination():
    from apps.killboard.views import _page_from_cache

    page = _page_from_cache(rows=[object()] * 50, count=120, number=1, per_page=50)
    assert page.paginator.num_pages == 3
    assert page.has_next() and not page.has_previous()
    assert len(list(page)) == 50


@pytest.mark.django_db
def test_killfeed_page_served_twice(client):
    cache.clear()
    _mk_killmail(killmail_id=30, is_npc=False)
    url = reverse("killboard:list")
    assert client.get(url).status_code == 200   # miss → builds + caches
    assert client.get(url).status_code == 200   # hit → served from the page cache


@pytest.mark.django_db
def test_killfeed_cache_key_is_not_query_param_injectable(client):
    """Raw ?kind=/?days= values must never reach the cache key, else an
    unauthenticated user could mint unbounded cache entries (memory-pressure DoS)."""
    from apps.killboard.views import _LIST_CACHE_VERSION, _home

    base = f"kb:list:{_LIST_CACHE_VERSION}:{_home()}"
    url = reverse("killboard:list")

    # A bogus kind is normalised to 'all' — the raw value never keys the cache.
    cache.clear()
    client.get(url + "?kind=zzz")
    assert cache.get(f"{base}:all:all:1") is not None   # cached under normalised key

    # A non-standard (still-valid) window is NOT cached at all — bounded keyspace.
    cache.clear()
    client.get(url + "?days=999")
    assert cache.get(f"{base}:all:999:1") is None        # never cached
    assert cache.get(f"{base}:all:all:1") is None        # and not under a fallback key

    # A standard window + valid kind still caches under its normalised key.
    cache.clear()
    client.get(url + "?kind=kills&days=7")
    assert cache.get(f"{base}:kills:7:1") is not None


# --- A.3 Perf indexes declared ----------------------------------------------
def test_perf_indexes_declared():
    names = {idx.name for idx in Killmail._meta.indexes}
    assert "km_home_role_time_idx" in names
    assert "km_victim_ship_idx" in names
    from apps.killboard.models import KillmailParticipant

    kp_names = {idx.name for idx in KillmailParticipant._meta.indexes}
    assert "kp_role_corp_char_idx" in kp_names


# --- B Configurable EVE-image origin ----------------------------------------
def test_image_tags_follow_base_setting():
    from apps.sde.templatetags import eve

    with override_settings(EVE_IMAGE_BASE_URL="/eveimg"):
        assert eve.eve_portrait(95465499, 64) == "/eveimg/characters/95465499/portrait?size=64"
        assert eve.eve_type_icon(34, 32) == "/eveimg/types/34/icon?size=32"
    with override_settings(EVE_IMAGE_BASE_URL="https://images.evetech.net"):
        assert eve.eve_type_render(587) == "https://images.evetech.net/types/587/render?size=512"


def test_csp_img_src_follows_base_setting():
    from core.middleware import _build_csp

    with override_settings(EVE_IMAGE_BASE_URL="/eveimg"):
        csp = _build_csp("testnonce")
        assert "img-src 'self' data:" in csp
        assert "images.evetech.net" not in csp
    with override_settings(EVE_IMAGE_BASE_URL="https://images.evetech.net"):
        assert "img-src 'self' https://images.evetech.net data:" in _build_csp("testnonce")
