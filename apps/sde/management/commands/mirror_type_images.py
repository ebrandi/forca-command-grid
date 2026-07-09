"""Mirror EVE type icons (and ship renders) to a local directory nginx serves.

Type images are a finite, static set keyed by ``type_id`` (they only change when
CCP adds/updates items), so we can self-host them outright instead of proxying
every request: this downloads them once from CCP's image server into
``EVE_IMAGE_MIRROR_DIR``, laid out so nginx can serve
``/eveimg/types/{id}/{kind}?size=N`` straight off disk and fall back to the proxy
for anything not mirrored (new types, odd sizes, and the inherently dynamic
portraits / corp / alliance logos).

Idempotent and resumable — existing files are skipped, so an interrupted run just
needs re-running. Per-image 404s (a type with no render, say) are recorded with a
small marker so re-runs don't re-request them.

    manage.py mirror_type_images                 # all published types, icons 32/64 + ship renders 512
    manage.py mirror_type_images --referenced-only
    manage.py mirror_type_images --limit 200     # smoke test
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.sde.models import SdeGroup, SdeType

SHIP_CATEGORY_ID = 6  # EVE inventory category for ships (renders only exist for these)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg"}
# CCP's image CDN rate-limits a busy source IP, so back off and retry on 429/5xx
# rather than abandoning the image (an abandoned one just isn't mirrored → proxy).
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
# Permanent "this type has no such image": 404 (type/image not found) and 400
# ("bad category or variation" — e.g. a blueprint has a `bp` image but no `icon`).
# Mark these so we never re-request them; the app never shows them anyway.
_NO_IMAGE_STATUS = {400, 404}


class Command(BaseCommand):
    help = "Download EVE type icons + ship renders to the local mirror nginx serves."

    def add_arguments(self, parser) -> None:
        # The sizes the app actually requests: icons at 32/64 (module & item lists),
        # renders at 64 (the dense killboard ship list) and 512 (detail pages).
        parser.add_argument("--sizes", default="32,64", help="icon sizes (comma list)")
        parser.add_argument("--render-sizes", default="64,512", help="ship render sizes")
        parser.add_argument("--no-renders", action="store_true", help="skip ship renders")
        parser.add_argument("--referenced-only", action="store_true",
                            help="only types seen on killmails/doctrines (smaller)")
        parser.add_argument("--limit", type=int, default=0, help="cap the type count (test)")
        parser.add_argument("--concurrency", type=int, default=6)
        parser.add_argument("--force", action="store_true", help="re-download existing files")

    def handle(self, *args, **opts) -> None:
        self.base = getattr(settings, "EVE_IMAGE_SOURCE_URL",
                            "https://images.evetech.net").rstrip("/")
        self.root = getattr(settings, "EVE_IMAGE_MIRROR_DIR", "eveimg")
        self.force = opts["force"]
        self.headers = {"User-Agent": settings.ESI_USER_AGENT}
        icon_sizes = [int(s) for s in opts["sizes"].split(",") if s.strip()]
        render_sizes = [int(s) for s in opts["render_sizes"].split(",") if s.strip()]

        ship_ids = set(
            SdeType.objects.filter(
                published=True,
                group_id__in=SdeGroup.objects.filter(category_id=SHIP_CATEGORY_ID)
                .values_list("group_id", flat=True),
            ).values_list("type_id", flat=True)
        )
        type_ids = self._type_ids(opts["referenced_only"])
        if opts["limit"]:
            type_ids = type_ids[: opts["limit"]]

        # Build the full job list of (type_id, kind, size).
        jobs: list[tuple[int, str, int]] = []
        for tid in type_ids:
            for size in icon_sizes:
                jobs.append((tid, "icon", size))
            if not opts["no_renders"] and tid in ship_ids:
                for size in render_sizes:
                    jobs.append((tid, "render", size))

        self.stdout.write(
            f"Mirroring {len(type_ids):,} types → {len(jobs):,} images "
            f"(icons {icon_sizes}, renders {render_sizes if not opts['no_renders'] else 'off'}) "
            f"from {self.base} into {self.root}"
        )
        saved = skipped = missing = errors = 0
        with ThreadPoolExecutor(max_workers=opts["concurrency"]) as pool:
            futures = {pool.submit(self._one, *job): job for job in jobs}
            for i, fut in enumerate(as_completed(futures), 1):
                result = fut.result()
                saved += result == "saved"
                skipped += result == "skip"
                missing += result == "missing"
                errors += result == "error"
                if i % 2000 == 0:
                    self.stdout.write(f"  …{i:,}/{len(jobs):,} "
                                      f"({saved} saved, {skipped} skipped, {missing} none, {errors} err)")

        self.stdout.write(self.style.SUCCESS(
            f"Done. {saved} saved, {skipped} already present, {missing} have no image, {errors} errors."
        ))

    def _type_ids(self, referenced_only: bool) -> list[int]:
        if referenced_only:
            from apps.killboard.models import Killmail, KillmailItem
            ids = set(KillmailItem.objects.values_list("item_type_id", flat=True).distinct())
            ids |= set(Killmail.objects.values_list("victim_ship_type_id", flat=True).distinct())
            ids |= set(SdeType.objects.filter(
                published=True,
                group_id__in=SdeGroup.objects.filter(category_id=SHIP_CATEGORY_ID)
                .values_list("group_id", flat=True),
            ).values_list("type_id", flat=True))
            return sorted(i for i in ids if i)
        return list(
            SdeType.objects.filter(published=True).order_by("type_id")
            .values_list("type_id", flat=True)
        )

    def _dir(self, type_id: int) -> str:
        return os.path.join(self.root, "types", str(type_id))

    def _one(self, type_id: int, kind: str, size: int) -> str:
        """Download one (type, kind, size). Returns saved|skip|missing|error."""
        base = os.path.join(self._dir(type_id), f"{kind}-{size}")
        if not self.force:
            if os.path.exists(base + ".png") or os.path.exists(base + ".jpg"):
                return "skip"
            if os.path.exists(base + ".404"):
                return "missing"
        url = f"{self.base}/types/{type_id}/{kind}?size={size}"
        resp = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
            except requests.RequestException:
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code in _RETRY_STATUS:
                # Honour Retry-After when given, else a small exponential back-off.
                try:
                    wait = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 1.5 * (attempt + 1)
                time.sleep(min(wait, 30))
                continue
            break
        if resp is None or resp.status_code in _RETRY_STATUS:
            return "error"  # never written → a later re-run retries it (proxy covers it meanwhile)
        if resp.status_code in _NO_IMAGE_STATUS:
            self._write(base + ".404", b"")  # marker: this type has no such image
            return "missing"
        if resp.status_code != 200:
            return "error"
        ext = _EXT.get(resp.headers.get("Content-Type", "").split(";")[0].strip().lower())
        if not ext:
            return "error"
        self._write(base + f".{ext}", resp.content)
        return "saved"

    def _write(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)  # atomic — a reader never sees a half-written file
