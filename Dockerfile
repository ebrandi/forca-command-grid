# [FORCA] Command Grid — application image (shared by web/worker/beat)
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps: libpq for psycopg; gettext for `compilemessages` (msgfmt) so the
# localisation catalogues can be compiled into the image. (Healthchecks use Python's
# urllib, so no curl is installed — keeping it out of the runtime image removes a
# ready-made SSRF/exfil tool for a post-exploitation attacker.)
# DL3008 (pin apt versions) is deliberately not applied: libpq5/gettext are Debian base-image
# OS packages tracked by the base image's own security updates. Pinning them to an exact apt
# version would break the build the moment the base image refreshes those versions, for
# negligible security gain — so we track the base image rather than pin.
# fonts-dejavu-core supplies DejaVuSans{,-Bold}.ttf for the Pillow-rendered kill-card /
# CV-card PNGs (KB-39). It is a tiny (~1MB), permissively-licensed font package; without
# it Pillow falls back to its low-res bitmap default and the shareable cards look poor.
# fonts-noto-cjk adds Chinese/Japanese/Korean glyph coverage for the Combat Signatures
# banners (DejaVu has none — CJK pilot names and localised zh-hans/ko/ja labels would render
# as tofu boxes). It is larger (~90MB) and SIL-OFL-1.1 licensed; the renderer degrades to
# DejaVu when it is absent, so a stale self-hosted image still renders (just without CJK).
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 gettext fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip itself to a patched release before installing anything: the base
# image's bundled pip had known CVEs (flagged by the dependency audit). Keep this
# floor moving as new pip advisories land.
RUN pip install --upgrade "pip>=26.1.2"

# Install Python deps first for layer caching.
COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

# App source
COPY . .

# Compile message catalogues (.po → .mo) into the image so translations are live at
# runtime and LOCALE_PATHS finds them. The build FAILS on a malformed catalogue — a
# broken locale must never ship silently (docs/i18n/design/11-catalogue-maintenance.md).
# With no catalogues yet this is a harmless no-op. Pinned to the base settings so the
# step needs no runtime env.
RUN DJANGO_SETTINGS_MODULE=config.settings.base python manage.py compilemessages

# Non-root runtime user
RUN useradd --system --create-home appuser \
    && mkdir -p /app/staticfiles /app/media /app/archive /app/eveimg \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Image-level health probe for when the image runs standalone as the web server (e.g. outside
# compose). Uses Python's urllib — no curl in the image (see above). The compose `web` service
# declares its own identical healthcheck; postgres/redis override with theirs; worker and beat
# do NOT serve HTTP on :8000, so they opt out via `healthcheck: disable: true` in
# docker-compose.prod.yml rather than inherit (and fail) this web-oriented check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"]

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
