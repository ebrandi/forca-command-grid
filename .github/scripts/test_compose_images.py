#!/usr/bin/env python3
"""Tests for compose_images.py — the parser image-scan.yml builds its matrix from.

WHY THESE EXIST AND WHY THEY ARE unittest, NOT pytest:
    If this parser silently returns a short list, the scan matrix shrinks and the workflow
    still reports green. That is the exact failure mode the whole workstream is a reaction
    to — a control that looks like coverage while covering nothing — so the parser needs a
    gate of its own.

    The repo's pytest config sets `testpaths = ["tests", "apps", "core"]`, so a test file
    under .github/ would never run in `pytest -q`, and a test that never runs is decoration.
    Writing them as unittest.TestCase means they run BOTH ways: the image-scan workflow
    executes them with stdlib `python3 -m unittest` before trusting the parser (no pytest
    install, no Django, no database), and a developer can still run
    `pytest .github/scripts/test_compose_images.py` locally, because pytest collects
    unittest classes natively.

Run: python3 .github/scripts/test_compose_images.py
     (or `pytest .github/scripts/test_compose_images.py`, which needs the explicit path —
     `pytest -q` alone will not collect this file, by design: it must not contend for the
     shared test database, and it needs neither Django nor Postgres.)
"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # noqa: S404 - runs this repo's own script, no external input
import sys
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).with_name("compose_images.py")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    """Import compose_images.py by path — .github/scripts is not an importable package."""
    spec = importlib.util.spec_from_file_location("compose_images", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compose_images = _load_module()


class ThirdPartyImagesTests(unittest.TestCase):
    """The selection rule: pulled images in, locally-built images out, anything odd fatal."""

    def test_excludes_services_we_build_ourselves(self):
        """A `build:` service must not reach the matrix — pulling it would 404 or, worse,
        fetch an unrelated registry image that happens to share the name."""
        compose = yaml.safe_load(
            """
            services:
              web:
                build: {context: .}
                image: forca-command-grid:prod
              nginx:
                image: nginx:1.30-alpine
            """
        )
        self.assertEqual(compose_images.third_party_images(compose), ["nginx:1.30-alpine"])

    def test_resolves_the_x_app_merge_anchor(self):
        """web/worker/beat inherit `build:` and `image:` through `<<: *app`. A grep over
        `image:` lines would treat the built image as a pin; a YAML load must not."""
        compose = yaml.safe_load(
            """
            x-app: &app
              build: {context: .}
              image: forca-command-grid:prod
            services:
              web:
                <<: *app
              worker:
                <<: *app
                command: celery
              redis:
                image: redis:7-alpine
            """
        )
        self.assertEqual(compose_images.third_party_images(compose), ["redis:7-alpine"])

    def test_deduplicates_and_sorts(self):
        """Two services on one image is one scan, and a stable order keeps the matrix job
        names stable across runs."""
        compose = yaml.safe_load(
            """
            services:
              a: {image: redis:7-alpine}
              b: {image: redis:7-alpine}
              c: {image: nginx:1.30-alpine}
            """
        )
        self.assertEqual(
            compose_images.third_party_images(compose),
            ["nginx:1.30-alpine", "redis:7-alpine"],
        )

    def test_registry_host_with_port_is_not_mistaken_for_a_tag(self):
        """`registry.example.com:5000/thing` has a colon but no tag. Only the part after
        the last `/` may be inspected for one, or an unpinned image slips through."""
        compose = yaml.safe_load(
            "services:\n  a: {image: 'registry.example.com:5000/thing'}\n"
        )
        with self.assertRaises(SystemExit):
            compose_images.third_party_images(compose)

    def test_digest_pin_is_accepted(self):
        digest = "ghcr.io/x/y@sha256:" + "a" * 64
        compose = yaml.safe_load(f"services:\n  a: {{image: '{digest}'}}\n")
        self.assertEqual(compose_images.third_party_images(compose), [digest])

    def test_untagged_image_is_fatal(self):
        """`nginx` means `nginx:latest` — a moving target that makes every result
        unreproducible, so it is a build stop rather than a silent scan of who-knows-what."""
        compose = yaml.safe_load("services:\n  a: {image: nginx}\n")
        with self.assertRaises(SystemExit):
            compose_images.third_party_images(compose)

    def test_service_with_neither_build_nor_image_is_fatal(self):
        compose = yaml.safe_load("services:\n  a: {command: sleep}\n")
        with self.assertRaises(SystemExit):
            compose_images.third_party_images(compose)

    def test_no_services_is_fatal(self):
        with self.assertRaises(SystemExit):
            compose_images.third_party_images({"services": {}})

    def test_all_services_built_locally_is_fatal_not_empty(self):
        """An empty matrix would make the scan job vanish and the workflow report success.
        Returning nothing must never be a pass."""
        compose = yaml.safe_load(
            "services:\n  web: {build: {context: .}, image: forca-command-grid:prod}\n"
        )
        with self.assertRaises(SystemExit):
            compose_images.third_party_images(compose)


class RealComposeFileTests(unittest.TestCase):
    """End-to-end against the actual production compose file, invoked exactly as the
    workflow invokes it. This is what catches a rename or a restructure of the real file."""

    def test_script_emits_the_production_pins_as_a_github_output_line(self):
        result = subprocess.run(  # noqa: S603 - fixed argv, this repo's own script
            [sys.executable, str(SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        line = result.stdout.strip()
        self.assertTrue(line.startswith("images="), line)
        images = json.loads(line[len("images=") :])

        # The three services the stack pulls. Asserted as an exact set, not a subset: an
        # image silently dropping out of the matrix is the failure being guarded against.
        self.assertEqual(
            set(images),
            {"nginx:1.30-alpine", "postgres:16-alpine", "redis:7-alpine"},
        )
        # ...and the image we build must not be in there.
        self.assertNotIn("forca-command-grid:prod", images)

    def test_running_outside_the_repo_root_fails_loudly(self):
        """The workflow runs this from the checkout root. If that ever changes, the script
        must stop rather than emit an empty matrix."""
        result = subprocess.run(  # noqa: S603 - fixed argv, this repo's own script
            [sys.executable, str(SCRIPT)],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
