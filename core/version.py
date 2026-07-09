"""Resolve the deployed source revision, for display in the footer.

The production image is built from a tarball that excludes ``.git`` (so there is
no runtime git checkout). To keep the footer honest in every environment we
resolve the commit from the first available source, in order:

1. the ``GIT_COMMIT`` environment variable (set at build/deploy time);
2. a ``.git-commit`` stamp file written next to the source at package time
   (the production source of truth — written by the deploy step);
3. a live ``git rev-parse`` for developer checkouts that do have ``.git``.

Returns an empty string when the revision can't be determined, so the template
can simply hide the line.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

# core/ lives directly under the repo root; the stamp file sits at that root.
_BASE_DIR = Path(__file__).resolve().parent.parent
_STAMP = _BASE_DIR / ".git-commit"
_LEN = 12


@lru_cache(maxsize=1)
def git_commit() -> str:
    """Short commit hash of the deployed source ('' if unknown). Cached per process."""
    env = os.environ.get("GIT_COMMIT", "").strip()
    if env:
        return env[:_LEN]

    try:
        stamped = _STAMP.read_text(encoding="utf-8").strip()
        if stamped:
            return stamped[:_LEN]
    except OSError:
        pass

    git_bin = shutil.which("git")
    if git_bin:
        try:
            # Fixed, trusted argument vector (no user input); resolved absolute path.
            out = subprocess.run(  # noqa: S603
                [git_bin, "rev-parse", f"--short={_LEN}", "HEAD"],
                cwd=_BASE_DIR,
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return ""
