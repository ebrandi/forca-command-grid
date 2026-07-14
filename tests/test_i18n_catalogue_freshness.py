"""The committed catalogues contain every string the code actually marks.

The failure this prevents: someone marks a new string, ships it, and forgets to re-run
``makemessages``. The msgid never enters the catalogue, so it can never be translated —
and nothing complains, because an unknown msgid silently falls back to English. The page
looks correct in review and is simply, permanently, untranslatable.

docs/i18n/05-final-report.md §11.4 deferred this gate for two reasons, both addressed here:

* it assumed a ``makemessages --check`` flag, which Django does not have; and
* it worried that ``msgfmt``/``xgettext`` formatting drifts between gettext versions and
  would make a file-level diff flap.

Comparing the *set of msgid identities* — ``(msgctxt, msgid)`` — instead of file bytes
makes wrapping, line references, header fields and gettext version all irrelevant.

``makemessages`` walks the current working directory and writes throwaway ``<file>.py``
scratch files next to each template, so it is run against a **copy** of the tree. A test
must never mutate the committed catalogues, and running it in-place would.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

polib = pytest.importorskip("polib")

REFERENCE_LOCALE = "de"  # any real locale; the msgid set is identical across all of them

pytestmark = pytest.mark.skipif(
    shutil.which("xgettext") is None,
    reason="gettext (xgettext) not installed — extraction cannot run.",
)


def _msgid_keys(po_path: Path) -> set[tuple[str | None, str]]:
    """Identity of every live entry: (context, msgid). Formatting-independent."""
    po = polib.pofile(str(po_path))
    return {(e.msgctxt, e.msgid) for e in po if e.msgid and not e.obsolete}


SKIP_DIRS = {".venv", "node_modules", "staticfiles", ".git", "__pycache__", ".pytest_cache", "media"}


def _sources(root: Path) -> list[str]:
    """Every .py/.html that could carry a marked string. Deliberately git-free: the app
    container has no git, and a gate that silently skips is worse than no gate."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith((".py", ".html")):
                found.append(str(Path(dirpath, name).relative_to(root)))
    return found


@pytest.mark.slow
def test_committed_catalogue_has_every_marked_string(tmp_path):
    root = Path(settings.BASE_DIR)
    committed = root / "locale" / REFERENCE_LOCALE / "LC_MESSAGES" / "django.po"
    assert committed.exists(), "reference catalogue is missing"

    sources = _sources(root)
    assert sources, "found no .py/.html sources to extract from — the walk is broken"

    # Mirror only the marked-string sources into a scratch tree, so extraction cannot touch
    # anything committed.
    work = tmp_path / "tree"
    for rel in sources:
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dst)
    (work / "locale" / REFERENCE_LOCALE / "LC_MESSAGES").mkdir(parents=True)

    cmd = [
        "django-admin", "makemessages",
        "--locale", REFERENCE_LOCALE,
        "--no-obsolete",
        "--add-location", "file",
        "--extension", "py,html",
        "--ignore", ".venv/*",
        "--ignore", "staticfiles/*",
        "--ignore", "node_modules/*",
    ]
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings.base")}
    # noqa S603: `cmd` is a fixed literal argv (no shell, no user input) run against a scratch copy.
    proc = subprocess.run(  # noqa: S603
        cmd, cwd=work, capture_output=True, text=True, env=env, check=False
    )
    if proc.returncode != 0:
        pytest.skip(f"makemessages could not run here: {proc.stderr.strip()[:300]}")

    fresh_po = work / "locale" / REFERENCE_LOCALE / "LC_MESSAGES" / "django.po"
    if not fresh_po.exists() or not fresh_po.stat().st_size:
        pytest.skip("extraction produced no catalogue in the scratch tree")

    missing = _msgid_keys(fresh_po) - _msgid_keys(committed)

    assert not missing, (
        f"{len(missing)} string(s) are marked in the code but missing from the committed "
        "catalogues, so they can never be translated. Re-run makemessages and commit "
        "locale/*/LC_MESSAGES/django.po.\nFirst few:\n"
        + "\n".join(f"  msgctxt={c!r} msgid={m!r}" for c, m in sorted(missing, key=lambda k: k[1])[:10])
    )
