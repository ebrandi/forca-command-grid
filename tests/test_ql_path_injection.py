"""Combat Signatures — the background asset path is confined to the background tree.

Pins :func:`signature_assets.background_file_path`, the single derivation of a design's file path,
and the renderer's use of it. The rule under test is narrow and absolute: whatever a caller passes
as a background key, the opened file is a direct child of ``sigbg_dir()`` or there is no file at
all — because ``pathlib`` composes paths without confining them (``root / "../.."`` climbs out of
the tree and ``root / "/etc"`` silently discards ``root`` altogether).

Today every key reaching the renderer comes off a ``SignatureBackground`` row seeded from the
committed manifest, so none of these inputs is reachable from a request. These tests exist so that
stays true by construction: if the validation is ever dropped — or the key ever becomes a request
parameter — the escape shows up here as a failure rather than as an arbitrary-file read.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.killboard.signature_assets import (
    FILE_KEYS,
    PRESETS,
    background_file_path,
    load_manifest,
    sigbg_dir,
)
from apps.killboard.signature_render import _load_background, render_signature_png

DEVICE = (PRESETS["standard"][0] * 2, PRESETS["standard"][1] * 2)

# Keys that must never resolve to a path: parent traversal in every shape, absolute paths (the
# pathlib surprise), separators, NUL, an over-long key and the empty string.
HOSTILE_KEYS = (
    "..",
    "../..",
    "../../etc",
    "..%2f..",
    "nebula-emberfront/../../..",
    "./nebula-emberfront",
    "sub/dir",
    "sub\\dir",
    "/etc",
    "/etc/ssl/private",
    "C:\\Windows",
    "nebula-emberfront\n",
    "\x00nebula-emberfront",
    "a" * 65,
    "",
)


@pytest.fixture
def outside_png(tmp_path):
    """A readable PNG in a directory that is NOT under the background tree.

    Named for a real preset so nothing but the path rule stands between a hostile key and it.
    """
    from PIL import Image

    target = tmp_path / "outside"
    target.mkdir()
    Image.new("RGB", PRESETS["standard"], (255, 0, 255)).save(target / "standard.png")
    return target


# --------------------------------------------------------------------------- #
#  Escapes
# --------------------------------------------------------------------------- #
def test_absolute_key_cannot_pull_a_file_from_outside_the_tree(outside_png):
    """An absolute key is the sharp case: ``Path("/a") / "/tmp/x"`` IS ``/tmp/x``."""
    naive = sigbg_dir() / str(outside_png) / "standard.png"
    assert naive == outside_png / "standard.png", "pathlib discards the left side — precondition"
    assert naive.exists()

    assert background_file_path(str(outside_png), "standard") is None
    assert _load_background(str(outside_png), "standard", DEVICE) is None


def test_dotdot_key_cannot_climb_out_of_the_tree(outside_png):
    """A relative key of ``../../..`` reaches the same file by climbing instead of by anchoring."""
    relative = os.path.relpath(outside_png, sigbg_dir())
    assert relative.startswith(".."), "precondition: the target really is outside the tree"
    assert (sigbg_dir() / relative / "standard.png").exists()

    assert background_file_path(relative, "standard") is None
    assert _load_background(relative, "standard", DEVICE) is None


@pytest.mark.parametrize("key", HOSTILE_KEYS)
def test_hostile_keys_never_produce_a_path(key):
    assert background_file_path(key, "standard") is None


@pytest.mark.parametrize("key", HOSTILE_KEYS)
def test_hostile_keys_never_produce_a_background(key):
    assert _load_background(key, "standard", DEVICE) is None


def test_file_name_is_a_fixed_enum_not_a_string():
    """The second segment is chosen from ``FILE_KEYS``; nothing else names a file."""
    for name in ("../manage", "manifest", "manifest.json", "standard.png", "", "STANDARD"):
        assert background_file_path("nebula-emberfront", name) is None
    for name in FILE_KEYS:
        assert background_file_path("nebula-emberfront", name) is not None


@pytest.mark.parametrize("bad", [None, 1, b"nebula-emberfront", Path("/etc")])
def test_non_string_arguments_are_rejected_not_coerced(bad):
    assert background_file_path(bad, "standard") is None
    assert background_file_path("nebula-emberfront", bad) is None


def test_every_result_stays_inside_the_background_tree():
    """The containment invariant, stated once over every input this module accepts."""
    root = Path(os.path.normpath(sigbg_dir()))
    candidates = [*HOSTILE_KEYS, *(b["key"] for b in load_manifest()["backgrounds"])]
    for key in candidates:
        for name in (*FILE_KEYS, "../escape", "manifest.json"):
            path = background_file_path(key, name)
            if path is None:
                continue
            assert path.is_relative_to(root)
            assert path.parent.parent == root, "a key names a DIRECT child, never a subtree"


# --------------------------------------------------------------------------- #
#  The guard must not cost a legitimate signature anything
# --------------------------------------------------------------------------- #
def test_every_manifest_key_still_resolves_and_loads():
    """The shipped catalogue is unaffected: each committed design still opens at each preset."""
    keys = [bg["key"] for bg in load_manifest()["backgrounds"]]
    assert keys, "precondition: the manifest ships designs"
    for key in keys:
        for preset in PRESETS:
            device = (PRESETS[preset][0] * 2, PRESETS[preset][1] * 2)
            assert background_file_path(key, preset) == sigbg_dir() / key / f"{preset}.png"
            assert _load_background(key, preset, device) is not None, f"{key}/{preset}"


def test_unknown_but_legal_key_is_simply_no_background():
    """A well-formed key with no committed file behaves exactly as before — None, not an error."""
    assert background_file_path("no-such-design", "standard") is not None
    assert _load_background("no-such-design", "standard", DEVICE) is None


# --------------------------------------------------------------------------- #
#  The preset reaching the filesystem is validated before the render starts
# --------------------------------------------------------------------------- #
def test_render_rejects_an_unknown_size_preset_before_touching_disk():
    """``render_signature_png`` resolves the preset through ``PRESETS`` first.

    That lookup is the guard that keeps ``size_preset`` a fixed enum on the render path; pinning it
    means removing it (e.g. defaulting to some fallback size) fails here rather than quietly handing
    a caller-supplied string to the asset loader.
    """
    payload = {
        "signature_id": 1,
        "background_key": "nebula-emberfront",
        "size_preset": "../../../etc",
        "layout": "identity",
        "theme": "gold",
        "components": [],
        "labels": {},
    }
    with pytest.raises(KeyError):
        render_signature_png(None, payload)
