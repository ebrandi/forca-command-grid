"""mirror_type_images: download type icons/ship renders to the local mirror.

CCP's image server is mocked (responses), so these are fast and offline.
"""
from __future__ import annotations

import os

import pytest
import responses
from django.core.management import call_command
from django.test import override_settings

from apps.sde.models import SdeCategory, SdeGroup, SdeType

SHIP, MODULE = 6, 7
PNG = b"\x89PNG\r\n\x1a\n"
JPG = b"\xff\xd8\xff\xe0"


@pytest.fixture
def types(db):
    ship_cat = SdeCategory.objects.create(category_id=SHIP, name="Ship")
    mod_cat = SdeCategory.objects.create(category_id=MODULE, name="Module")
    ship_grp = SdeGroup.objects.create(group_id=10, category=ship_cat, name="Frigate")
    mod_grp = SdeGroup.objects.create(group_id=20, category=mod_cat, name="Gun")
    SdeType.objects.create(type_id=1001, group=ship_grp, name="TestShip", published=True)
    SdeType.objects.create(type_id=1002, group=mod_grp, name="TestModule", published=True)
    SdeType.objects.create(type_id=1003, group=mod_grp, name="Unpublished", published=False)


def _img(type_id, kind, body=PNG, ctype="image/png", status=200):
    responses.add(responses.GET, f"https://images.evetech.net/types/{type_id}/{kind}",
                  body=body, content_type=ctype, status=status)


def _run(tmp, **kw):
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp), ESI_USER_AGENT="test/1.0"):
        call_command("mirror_type_images", "--concurrency", "1", *(kw.get("args") or []))


@responses.activate
def test_mirrors_icons_and_ship_renders(types, tmp_path):
    for t in (1001, 1002):
        _img(t, "icon")
    _img(1001, "render")  # only the ship has a render
    _run(tmp_path)

    # Icons for both published types at both default sizes.
    for t in (1001, 1002):
        for size in (32, 64):
            p = tmp_path / "types" / str(t) / f"icon-{size}.png"
            assert p.exists() and p.read_bytes() == PNG
    # Ship render present, module render never requested.
    assert (tmp_path / "types" / "1001" / "render-512.png").exists()
    assert not (tmp_path / "types" / "1002" / "render-512.png").exists()
    # Unpublished type skipped entirely.
    assert not (tmp_path / "types" / "1003").exists()


@responses.activate
def test_content_type_decides_extension(types, tmp_path):
    _img(1001, "icon")  # the command also fetches the other published type
    _img(1002, "icon", body=JPG, ctype="image/jpeg")
    _run(tmp_path, args=["--no-renders", "--limit", "0"])
    assert (tmp_path / "types" / "1002" / "icon-32.jpg").exists()
    assert not (tmp_path / "types" / "1002" / "icon-32.png").exists()


@responses.activate
def test_400_bad_variation_is_treated_as_no_image(types, tmp_path):
    # CCP returns 400 "bad category or variation" for types with no icon (e.g. a
    # blueprint, which has a `bp` image but no `icon`). Treat it like a 404.
    _img(1001, "icon")
    _img(1002, "icon", body=b"bad category or variation",
         ctype="text/plain", status=400)
    _run(tmp_path, args=["--no-renders"])
    assert (tmp_path / "types" / "1002" / "icon-32.404").exists()
    assert not (tmp_path / "types" / "1002" / "icon-32.png").exists()


@responses.activate
def test_404_writes_marker_and_is_idempotent(types, tmp_path):
    _img(1001, "icon")  # exists
    _img(1002, "icon", status=404)  # has no icon
    _run(tmp_path, args=["--no-renders"])
    assert (tmp_path / "types" / "1002" / "icon-32.404").exists()
    # Re-run: the marker means we don't re-request (no new matched call needed).
    calls_before = len(responses.calls)
    _run(tmp_path, args=["--no-renders"])
    # The 404'd icon isn't re-requested; only types still missing would be.
    assert len(responses.calls) == calls_before  # nothing re-fetched


@responses.activate
def test_existing_files_are_skipped(types, tmp_path):
    _img(1001, "icon")
    _img(1002, "icon")
    _run(tmp_path, args=["--no-renders"])
    n1 = len(responses.calls)
    assert n1 > 0
    # Second run downloads nothing new.
    _run(tmp_path, args=["--no-renders"])
    assert len(responses.calls) == n1


@responses.activate
def test_limit_caps_type_count(types, tmp_path):
    _img(1001, "icon")  # only the first type (1001) is fetched under --limit 1
    _run(tmp_path, args=["--limit", "1", "--no-renders"])
    # Only the first published type (1001) by type_id order got icons.
    assert (tmp_path / "types" / "1001" / "icon-32.png").exists()
    assert not (tmp_path / "types" / "1002").exists()


@responses.activate
def test_referenced_only_includes_killmail_items(types, tmp_path):
    from django.utils import timezone

    from apps.killboard.models import Killmail, KillmailItem
    km = Killmail.objects.create(
        killmail_id=1, killmail_time=timezone.now(), solar_system_id=30000142,
        victim_ship_type_id=1001,
    )
    KillmailItem.objects.create(killmail=km, idx=0, item_type_id=1002, quantity_destroyed=1)
    _img(1001, "icon")
    _img(1002, "icon")
    _img(1001, "render")
    _run(tmp_path, args=["--referenced-only"])
    assert (tmp_path / "types" / "1001" / "icon-32.png").exists()  # victim ship
    assert (tmp_path / "types" / "1002" / "icon-32.png").exists()  # cargo/fit item


def test_atomic_write_leaves_no_tmp(types, tmp_path):
    from apps.sde.management.commands.mirror_type_images import Command
    cmd = Command()
    path = os.path.join(tmp_path, "types", "9", "icon-64.png")
    cmd._write(path, PNG)
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
