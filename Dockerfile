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
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 gettext \
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

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
