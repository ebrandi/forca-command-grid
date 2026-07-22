"""Combat Signatures — WS-2 background-library tests.

Guards the committed procedural background library end to end: the manifest parses and points at
files that exist at the right dimensions with matching sha256 checksums; every design keeps its
safe zones legible (``text_zone_ok``) at every preset; the generator is deterministic (regenerating
one design into a temp dir reproduces the committed bytes exactly); and the DB seed/sync keeps the
``SignatureBackground`` rows in step with the manifest, retiring (never deleting) dropped keys while
preserving an admin's enable/disable choice.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from django.core.management import call_command
from PIL import Image

from apps.killboard.models import SignatureBackground
from apps.killboard.signature_assets import (
    FILE_KEYS,
    THUMB_KEY,
    load_manifest,
    preset_size,
    sigbg_dir,
    sync_from_manifest,
    text_zone_ok,
)

MIN_DESIGNS = 22


@pytest.fixture(scope="module")
def manifest():
    return load_manifest()


def _committed_file(key: str, name: str) -> Path:
    return sigbg_dir() / key / f"{name}.png"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
#  Manifest shape & coverage
# --------------------------------------------------------------------------- #
def test_manifest_parses_and_is_well_formed(manifest):
    assert manifest["generator_version"]
    assert manifest["generated"] == "2026-07-22"
    backgrounds = manifest["backgrounds"]
    assert len(backgrounds) >= MIN_DESIGNS
    keys = [bg["key"] for bg in backgrounds]
    assert len(keys) == len(set(keys)), "duplicate background keys in manifest"
    for bg in backgrounds:
        for field in ("key", "name", "category", "seed", "display_order", "version", "checksum"):
            assert bg[field] not in (None, ""), f"{bg.get('key')} missing {field}"
        assert bg["provenance"]["origin"] == "procedural-original"
        assert "MIT" in bg["provenance"]["license"]
        assert set(bg["files"]) == set(FILE_KEYS)


def test_manifest_spans_all_six_categories(manifest):
    cats = {bg["category"] for bg in manifest["backgrounds"]}
    assert {"nebula", "fleet", "tactical", "warp", "weapons", "industrial"} <= cats


def test_manifest_seeds_are_distinct(manifest):
    seeds = [bg["seed"] for bg in manifest["backgrounds"]]
    assert len(seeds) == len(set(seeds)), "background seeds must be unique"


# --------------------------------------------------------------------------- #
#  Files exist, dimensions & checksums match (all of them — it's fast)
# --------------------------------------------------------------------------- #
def test_every_file_exists_with_correct_dimensions(manifest):
    for bg in manifest["backgrounds"]:
        for name in FILE_KEYS:
            path = _committed_file(bg["key"], name)
            assert path.exists(), f"missing {path}"
            with Image.open(path) as img:
                assert img.size == preset_size(name), f"{bg['key']}/{name}: {img.size}"


def test_every_checksum_matches_committed_bytes(manifest):
    for bg in manifest["backgrounds"]:
        for name in FILE_KEYS:
            path = _committed_file(bg["key"], name)
            assert _sha256(path) == bg["files"][name]["sha256"], f"checksum drift: {bg['key']}/{name}"


def test_thumb_exists_per_design(manifest):
    for bg in manifest["backgrounds"]:
        path = _committed_file(bg["key"], THUMB_KEY)
        assert path.exists()
        with Image.open(path) as img:
            assert img.size == preset_size(THUMB_KEY) == (200, 50)


# --------------------------------------------------------------------------- #
#  Contrast gate — every design keeps its safe areas legible at every preset
# --------------------------------------------------------------------------- #
def test_every_design_and_preset_passes_text_zone(manifest):
    failures = []
    for bg in manifest["backgrounds"]:
        for name in FILE_KEYS:
            with Image.open(_committed_file(bg["key"], name)) as img:
                if not text_zone_ok(img.convert("RGB")):
                    failures.append(f"{bg['key']}/{name}")
    assert not failures, f"safe-area contrast failed for: {failures}"


# --------------------------------------------------------------------------- #
#  Determinism — one design regenerated into a temp dir equals committed bytes
# --------------------------------------------------------------------------- #
def test_generator_is_deterministic_for_one_design(tmp_path, manifest):
    key = "tactical-radar"
    assert any(bg["key"] == key for bg in manifest["backgrounds"])
    call_command("generate_signature_backgrounds", only=key, out=str(tmp_path))
    for name in FILE_KEYS:
        regenerated = (tmp_path / key / f"{name}.png").read_bytes()
        committed = _committed_file(key, name).read_bytes()
        assert regenerated == committed, f"non-deterministic bytes: {key}/{name}"


# --------------------------------------------------------------------------- #
#  DB seed / sync
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_seed_migration_rows_match_manifest(manifest):
    manifest_keys = {bg["key"] for bg in manifest["backgrounds"]}
    db_keys = set(SignatureBackground.objects.values_list("key", flat=True))
    assert manifest_keys <= db_keys
    enabled = SignatureBackground.objects.filter(enabled=True).count()
    assert enabled >= MIN_DESIGNS


@pytest.mark.django_db
def test_seed_rows_carry_manifest_metadata(manifest):
    by_key = {bg["key"]: bg for bg in manifest["backgrounds"]}
    for row in SignatureBackground.objects.filter(key__in=by_key):
        bg = by_key[row.key]
        assert row.name == bg["name"]
        assert row.category == bg["category"]
        assert row.display_order == bg["display_order"]
        assert row.checksum == bg["checksum"]


@pytest.mark.django_db
def test_sync_retires_missing_key_without_deleting(manifest):
    orphan = SignatureBackground.objects.create(
        key="retired-test-key", name="Retired", category="nebula", enabled=True, display_order=999,
    )
    created, updated, retired = sync_from_manifest(manifest, SignatureBackground)
    assert created == 0  # already seeded by the migration
    assert retired >= 1
    orphan.refresh_from_db()
    assert orphan.enabled is False  # retired, not deleted
    assert SignatureBackground.objects.filter(key="retired-test-key").exists()


@pytest.mark.django_db
def test_sync_preserves_admin_disabled_choice(manifest):
    key = manifest["backgrounds"][0]["key"]
    SignatureBackground.objects.filter(key=key).update(enabled=False)
    sync_from_manifest(manifest, SignatureBackground)
    assert SignatureBackground.objects.get(key=key).enabled is False


@pytest.mark.django_db
def test_sync_creates_enabled_rows_on_fresh_db(manifest):
    SignatureBackground.objects.all().delete()
    created, updated, retired = sync_from_manifest(manifest, SignatureBackground)
    assert created == len(manifest["backgrounds"])
    assert SignatureBackground.objects.filter(enabled=True).count() == created
