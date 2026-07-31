#!/usr/bin/env python3
"""Emit the third-party image pins from docker-compose.prod.yml as a GitHub matrix.

WHY THIS IS A SCRIPT AND NOT A LIST IN THE WORKFLOW:
    .github/workflows/image-scan.yml has to know which images to scan. Writing them out
    there creates a second source of truth for "what runs in production", and that kind
    of duplication fails silently: bump nginx in the compose file, forget the workflow,
    and the job keeps scanning an image nobody deploys while reporting green. A control
    that is green for the wrong reason is worse than a missing one, because it stops
    anybody from looking. So the workflow asks the compose file, every run.

WHY IT PARSES YAML INSTEAD OF GREPPING `image:`:
    docker-compose.prod.yml defines web/worker/beat through the `x-app` anchor and pulls
    it in with a `<<:` merge key, so their `image:` and `build:` keys are not written on
    the service. A grep sees `forca-command-grid:prod` as just another pin and the
    workflow would then try to `docker pull` an image that only ever exists locally. A
    real YAML load resolves the merge and lets us apply the rule that actually matters:
    a service with a `build:` is one we produce ourselves, and it is scanned by the
    app-image job after being built — not pulled.

FAILURE BEHAVIOUR:
    Every problem here is a hard exit, never a shrug. An empty or partial list would
    hand the workflow a smaller matrix and still go green, which is precisely the
    silent-degradation mode this file exists to prevent.

Usage (from the repo root):
    python3 .github/scripts/compose_images.py >> "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

COMPOSE_FILE = Path("docker-compose.prod.yml")


def fail(message: str) -> None:
    """Abort loudly. Called for anything that would otherwise shrink the scan silently."""
    sys.stderr.write(f"compose_images: {message}\n")
    raise SystemExit(1)


def third_party_images(compose: dict) -> list[str]:
    """Return the sorted, de-duplicated images the stack PULLS rather than builds.

    A service is ours to build when it carries a `build:` key (directly or through the
    `x-app` anchor's merge). Those are excluded: the app-image job builds the tree's
    Dockerfile and scans the result, so pulling `forca-command-grid:prod` from a registry
    would either 404 or, worse, fetch some unrelated image with a colliding name.
    """
    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        fail(f"{COMPOSE_FILE} declares no services — refusing to emit an empty matrix.")

    images: set[str] = set()
    for name, service in services.items():
        if not isinstance(service, dict):
            fail(f"service {name!r} is not a mapping; cannot determine its image.")
        if service.get("build"):
            continue
        image = service.get("image")
        if not image:
            fail(f"service {name!r} has neither `build:` nor `image:` — cannot scan it.")
        # An unpinned image is its own finding: `nginx` alone means `nginx:latest`, which
        # is a moving target that makes every scan result unreproducible. The pins in this
        # file are all explicit today and must stay that way.
        if ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
            fail(f"service {name!r} pins {image!r} with no tag or digest — pin it.")
        images.add(image)

    if not images:
        fail(
            f"{COMPOSE_FILE} yielded no third-party images. Either every service is now "
            "built locally (in which case delete the service-images job deliberately) or "
            "the parse broke — both need a human, neither is a pass."
        )
    return sorted(images)


def main() -> None:
    if not COMPOSE_FILE.is_file():
        fail(f"{COMPOSE_FILE} not found (run this from the repository root).")
    try:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - malformed compose is a build stop
        fail(f"{COMPOSE_FILE} is not valid YAML: {exc}")
    if not isinstance(compose, dict):
        fail(f"{COMPOSE_FILE} did not parse to a mapping.")

    images = third_party_images(compose)
    # GitHub reads job outputs from key=value lines on $GITHUB_OUTPUT; the value must be a
    # single line, which compact JSON already is.
    sys.stdout.write("images=" + json.dumps(images, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
