#!/usr/bin/env bash
# Stamp the deployed source revision for the footer build marker.
#
# The Docker image is built with .git excluded, so there is no runtime git
# checkout. This writes the short commit hash into a `.git-commit` file next to
# the source, which the build copies in and the app reads (core/version.py) to
# show "build <hash>" in the footer. Run this immediately before building the
# image, on every deploy, so the footer always reflects the deployed commit.
#
# Usage: deploy/stamp-version.sh [SOURCE_DIR]
#   SOURCE_DIR defaults to the repository root (the parent of this script).
set -euo pipefail

src="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

if hash=$(git -C "${src}" rev-parse --short=12 HEAD 2>/dev/null); then
  printf '%s\n' "${hash}" > "${src}/.git-commit"
  echo "Stamped build revision ${hash}"
else
  # No git checkout (e.g. an unpacked tarball with no .git): leave any existing
  # stamp in place and let the footer hide the line if none exists.
  echo "stamp-version: ${src} is not a git checkout; skipping (footer will hide the build marker)" >&2
fi
