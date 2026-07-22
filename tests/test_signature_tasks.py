"""Combat Signatures — WS-4 Celery refresh-pipeline tests.

Exercises ``apps.killboard.signature_pipeline`` end to end under Celery-eager + locmem-cache test
settings: the inert-until-armed tick, coalesced dirty-marking from the KB-29 ring buffer, the
per-signature debounce, atomic last-known-good writes, the failure ledger + parking + regenerate,
interval refresh (live vs snapshot), the membership freeze/unfreeze/revoke sweep, the per-tick
render cap, cursor advance + prune fast-forward, and the media orphan janitor.

House style for image assertions (test_signature_render.py): structure only (PNG format + exact
preset dimensions), never pixel equality. All artifact writes go to a per-test ``MEDIA_ROOT`` and
the mirror is disabled (``EVE_IMAGE_MIRROR_DIR=""``) so nothing touches the network.
"""
from __future__ import annotations

import io
import os
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.killboard import signature_pipeline, signatures
from apps.killboard.models import (
    CombatSignature,
    CombatSignatureSettings,
    KillboardStreamEvent,
    SignatureBackground,
    SignatureScanState,
)
from apps.killboard.signature_render import PRESETS
from tests._raffle_utils import HOME_CORP, enrol_pilot, home_kill

pytestmark = pytest.mark.django_db

ACTIVE = CombatSignature.Status.ACTIVE
FROZEN = CombatSignature.Status.FROZEN
DISABLED = CombatSignature.Status.DISABLED
LIVE = CombatSignature.Mode.LIVE
SNAPSHOT = CombatSignature.Mode.SNAPSHOT
OK = CombatSignature.RenderStatus.OK
FAILED = CombatSignature.RenderStatus.FAILED
PENDING = CombatSignature.RenderStatus.PENDING


@pytest.fixture(autouse=True)
def _no_network(settings):
    """Keep every render offline: an unset mirror dir short-circuits every asset fetch to None."""
    settings.EVE_IMAGE_MIRROR_DIR = ""


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _background() -> SignatureBackground:
    bg, _created = SignatureBackground.objects.get_or_create(
        key="nebula-emberfront", defaults={"name": "Ember", "enabled": True}
    )
    return bg


def _enable(**over) -> CombatSignatureSettings:
    cfg = CombatSignatureSettings.load()
    cfg.enabled = True
    for key, value in over.items():
        setattr(cfg, key, value)
    cfg.save()
    return cfg


def _config(components=("pilot_name", "kills")) -> dict:
    return {
        "components": list(components), "period": "30d", "featured_trophy_ids": [],
        "show_timestamp": False, "theme": "gold",
    }


def _sig(char, *, status=ACTIVE, mode=LIVE, dirty=True, rendered_at=None,
         render_status=PENDING, consecutive_failures=0, components=("pilot_name", "kills"),
         snapshot_taken_at=None) -> CombatSignature:
    if mode == SNAPSHOT and snapshot_taken_at is None:
        snapshot_taken_at = timezone.now()
    return CombatSignature.objects.create(
        character=char, name="Sig", background=_background(), layout="identity",
        size_preset="standard", mode=mode, status=status, dirty=dirty, rendered_at=rendered_at,
        render_status=render_status, consecutive_failures=consecutive_failures,
        snapshot_taken_at=snapshot_taken_at, config=_config(components),
    )


def _kill_event(km_id, cid, *, when=None) -> KillboardStreamEvent:
    """A home-corp ATTACKER kill by ``cid`` plus its ring-buffer event (events carry no attacker
    id, so the pipeline must join back through the participant like the trophy scan)."""
    km = home_kill(km_id, attackers=[(cid, HOME_CORP, True)], when=when)
    return KillboardStreamEvent.objects.create(
        killmail=km, killmail_hash=km.killmail_hash, kill_time=km.killmail_time,
        home_role=km.home_corp_role, sec_band=km.sec_band or "lowsec",
        system_id=km.solar_system_id, ship_class="Frigate",
        victim_ship_type_id=km.victim_ship_type_id, victim_character_id=km.victim_character_id,
        victim_corporation_id=km.victim_corporation_id, total_value=km.total_value,
    )


def _artifact(sig) -> str:
    return signatures.artifact_path(sig.public_token)


def _write_dummy_artifact(sig, data=b"last-good") -> str:
    path = _artifact(sig)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _is_png(path) -> bool:
    from PIL import Image

    with open(path, "rb") as fh:
        img = Image.open(io.BytesIO(fh.read()))
    return img.format == "PNG" and img.size == PRESETS["standard"]


# --------------------------------------------------------------------------- #
#  Inert-until-armed
# --------------------------------------------------------------------------- #
def test_tick_disabled_is_a_noop(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    # Feature settings default to enabled=False → the tick returns before any work.
    _user, char = enrol_pilot(django_user_model, 8000)
    sig = _sig(char)  # dirty + active, but the feature is dark
    res = signature_pipeline.signature_tick()
    assert res == {"status": "disabled"}
    sig.refresh_from_db()
    assert sig.render_status == PENDING and sig.rendered_at is None
    assert not os.path.exists(_artifact(sig))


# --------------------------------------------------------------------------- #
#  Dirty-marking + coalescing from the stream
# --------------------------------------------------------------------------- #
def test_fresh_kill_marks_and_renders_then_coalesces(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8001)
    # A clean, recently-rendered banner: only a fresh kill (not the interval) should refresh it.
    sig = _sig(char, dirty=False, rendered_at=timezone.now() - timedelta(minutes=1),
               render_status=OK)
    _kill_event(1, char.character_id)

    res = signature_pipeline.signature_tick()
    assert res["marked_dirty"] == 1 and res["rendered"] == 1
    sig.refresh_from_db()
    assert sig.dirty is False and sig.render_status == OK
    assert _is_png(_artifact(sig))
    rendered_first = sig.rendered_at

    # A second tick with no new events must NOT re-render (coalescing + not dirty + inside interval).
    assert signature_pipeline.signature_tick()["rendered"] == 0
    sig.refresh_from_db()
    assert sig.rendered_at == rendered_first


def test_multiple_events_one_render(settings, tmp_path, django_user_model, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8002)
    sig = _sig(char, dirty=False, rendered_at=timezone.now() - timedelta(minutes=1),
               render_status=OK)
    for km_id in (1, 2, 3):  # three fresh kills, same pilot
        _kill_event(km_id, char.character_id)

    calls: list = []
    real = signature_pipeline.render_signature_png
    monkeypatch.setattr(
        signature_pipeline, "render_signature_png",
        lambda s, p, **kw: (calls.append(s.pk), real(s, p, **kw))[1],
    )
    res = signature_pipeline.signature_tick()
    assert res["rendered"] == 1 and calls == [sig.pk]  # coalesced to exactly one render


# --------------------------------------------------------------------------- #
#  Debounce + atomicity
# --------------------------------------------------------------------------- #
def test_debounce_second_call_skips(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8100)
    sig = _sig(char)
    assert signature_pipeline.render_one(sig.pk) == "rendered"
    assert signature_pipeline.render_one(sig.pk) == "skipped_debounce"


def test_atomic_write_is_complete_and_leaves_no_tmp(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8101)
    sig = _sig(char)
    assert signature_pipeline.render_one(sig.pk) == "rendered"
    path = _artifact(sig)
    assert _is_png(path)  # a fully-written, valid PNG
    assert not any(n.endswith(".tmp") for n in os.listdir(os.path.dirname(path)))


# --------------------------------------------------------------------------- #
#  Failure ledger: last-good retention, sanitised error, parking, regenerate
# --------------------------------------------------------------------------- #
def test_failure_keeps_last_good_parks_then_force_recovers(
    settings, tmp_path, django_user_model, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.SIGNATURE_RENDER_MAX_FAILURES = 3
    _enable()
    _user, char = enrol_pilot(django_user_model, 8200)
    sig = _sig(char)

    # 1) A first, real render establishes the last-known-good artifact.
    assert signature_pipeline.render_one(sig.pk) == "rendered"
    path = _artifact(sig)
    good = open(path, "rb").read()

    # 2) Now every render explodes with a path-bearing message.
    real = signature_pipeline.render_signature_png
    tmp_path_str = f"/srv/media/signatures/{sig.public_token}.png.tmp"

    def boom(_s, _p, **_kw):
        raise RuntimeError("compositor exploded at " + tmp_path_str)

    monkeypatch.setattr(signature_pipeline, "render_signature_png", boom)
    for _ in range(3):  # == SIGNATURE_RENDER_MAX_FAILURES
        cache.delete(signature_pipeline._debounce_key(sig))  # bypass the debounce each attempt
        assert signature_pipeline.render_one(sig.pk) == "failed"

    sig.refresh_from_db()
    assert sig.render_status == FAILED and sig.consecutive_failures == 3
    assert open(path, "rb").read() == good  # last-good retained, never truncated
    assert "/srv" not in sig.render_error and ".tmp" not in sig.render_error
    assert sig.public_token not in sig.render_error and "compositor exploded" in sig.render_error

    # 3) Parked: with a working renderer restored AND the debounce cleared, the tick's picker still
    #    skips it because it has hit the failure ceiling (dirty is irrelevant now).
    monkeypatch.setattr(signature_pipeline, "render_signature_png", real)
    cache.delete(signature_pipeline._debounce_key(sig))
    CombatSignature.objects.filter(pk=sig.pk).update(dirty=True)
    assert signature_pipeline.signature_tick()["rendered"] == 0
    sig.refresh_from_db()
    assert sig.render_status == FAILED  # untouched — the gate, not the debounce, skipped it

    # 4) A manual regenerate resets the ledger and renders.
    assert signature_pipeline.force_render(sig.pk) == "rendered"
    sig.refresh_from_db()
    assert sig.consecutive_failures == 0 and sig.render_status == OK and sig.render_error == ""


# --------------------------------------------------------------------------- #
#  Interval refresh — live re-renders, snapshot does not
# --------------------------------------------------------------------------- #
def test_interval_refreshes_live_but_not_snapshot(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    cfg = _enable()
    stale = timezone.now() - timedelta(hours=cfg.refresh_interval_hours + 1)
    _user, char = enrol_pilot(django_user_model, 8300)
    live = _sig(char, dirty=False, rendered_at=stale, render_status=OK)
    snap = _sig(char, mode=SNAPSHOT, dirty=False, rendered_at=stale, render_status=OK)

    res = signature_pipeline.signature_tick()
    assert res["rendered"] == 1  # only the live banner is interval-due
    live.refresh_from_db()
    snap.refresh_from_db()
    assert live.rendered_at > stale  # re-rendered
    assert snap.rendered_at == stale  # a snapshot never interval-refreshes


# --------------------------------------------------------------------------- #
#  Membership lifecycle sweep
# --------------------------------------------------------------------------- #
def test_leaving_corp_freezes_and_keeps_artifact(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()  # revoke_on_leave defaults False
    _user, char = enrol_pilot(django_user_model, 8400)
    sig = _sig(char, dirty=False, rendered_at=timezone.now(), render_status=OK)
    path = _write_dummy_artifact(sig)

    char.is_corp_member = False
    char.save(update_fields=["is_corp_member"])
    res = signature_pipeline.signature_tick()
    assert res["frozen"] == 1
    sig.refresh_from_db()
    assert sig.status == FROZEN
    assert os.path.exists(path)  # a frozen banner keeps its public image


def test_leaving_corp_revokes_when_configured(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable(revoke_on_leave=True)
    _user, char = enrol_pilot(django_user_model, 8401)
    sig = _sig(char, dirty=False, rendered_at=timezone.now(), render_status=OK)
    path = _write_dummy_artifact(sig)

    char.is_corp_member = False
    char.save(update_fields=["is_corp_member"])
    signature_pipeline.signature_tick()
    sig.refresh_from_db()
    assert sig.status == DISABLED
    assert not os.path.exists(path)  # revoke_on_leave deletes the image


def test_rejoining_unfreezes_and_rerenders(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8402)  # a current member
    sig = _sig(char, status=FROZEN, dirty=False, rendered_at=timezone.now(), render_status=OK)

    res = signature_pipeline.signature_tick()
    assert res["unfrozen"] == 1 and res["rendered"] == 1  # unfrozen THEN rendered, same tick
    sig.refresh_from_db()
    assert sig.status == ACTIVE and sig.render_status == OK and sig.dirty is False
    assert _is_png(_artifact(sig))


# --------------------------------------------------------------------------- #
#  Per-tick render cap
# --------------------------------------------------------------------------- #
def test_per_tick_cap_spreads_across_ticks(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.SIGNATURE_RENDER_MAX_PER_TICK = 2
    _enable()
    _user, char = enrol_pilot(django_user_model, 8500)
    for _ in range(5):
        _sig(char)  # 5 dirty, never-rendered banners

    assert signature_pipeline.signature_tick()["rendered"] == 2
    assert CombatSignature.objects.filter(render_status=OK).count() == 2
    assert signature_pipeline.signature_tick()["rendered"] == 2
    assert signature_pipeline.signature_tick()["rendered"] == 1
    assert CombatSignature.objects.filter(render_status=OK).count() == 5


# --------------------------------------------------------------------------- #
#  Scan cursor: advance + prune fast-forward
# --------------------------------------------------------------------------- #
def test_cursor_advances_past_consumed_events(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8600)
    _kill_event(1, char.character_id)
    ev2 = _kill_event(2, char.character_id)
    signature_pipeline.signature_tick()
    assert SignatureScanState.load().last_seq == ev2.seq


def test_cursor_fast_forwards_when_pruned(settings, tmp_path, django_user_model, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path)
    _enable()
    _user, char = enrol_pilot(django_user_model, 8601)
    ev = _kill_event(1, char.character_id)
    state = SignatureScanState.load()
    state.last_seq = ev.seq  # caught up
    state.save(update_fields=["last_seq"])
    # The ring buffer reports a tip beyond every retained event (a prune outran the cursor): the
    # batch query returns nothing, so the cursor must fast-forward to the tip without error.
    monkeypatch.setattr("apps.killboard.stream.tip_seq", lambda: ev.seq + 100)
    res = signature_pipeline.signature_tick()
    assert res["status"] == "ok" and res["marked_dirty"] == 0
    assert SignatureScanState.load().last_seq == ev.seq + 100


# --------------------------------------------------------------------------- #
#  Orphan cleanup janitor
# --------------------------------------------------------------------------- #
def test_cleanup_orphans_deletes_orphans_keeps_live(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    directory = os.path.join(str(tmp_path), "signatures")
    os.makedirs(directory)
    _user, char = enrol_pilot(django_user_model, 8700)
    active = _sig(char, status=ACTIVE)
    frozen = _sig(char, status=FROZEN)
    disabled = _sig(char, status=DISABLED)
    for sig in (active, frozen, disabled):
        _write_dummy_artifact(sig)
    unknown = "unknowntoken1234567890"  # 22 chars, valid token shape, no row
    open(os.path.join(directory, f"{unknown}.png"), "wb").close()
    foreign = os.path.join(directory, "not-a-signature.txt")
    open(foreign, "wb").close()

    removed = signature_pipeline.cleanup_orphans()
    assert removed == 2  # the disabled signature's image + the row-less token
    assert os.path.exists(_artifact(active)) and os.path.exists(_artifact(frozen))
    assert not os.path.exists(_artifact(disabled))
    assert not os.path.exists(os.path.join(directory, f"{unknown}.png"))
    assert os.path.exists(foreign)  # a non-token file is never touched


def test_cleanup_orphans_refuses_when_dir_missing(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)  # no signatures/ subdirectory
    assert signature_pipeline.cleanup_orphans() == -1
