"""KB-28 — the killboard REST API (DRF).

The first API surface in the repo. Read-only v1: session- or token-authenticated,
RBAC-tiered (member / officer / director), with an opt-in anonymous public-read subset
gated by ``settings.KILLBOARD_API_PUBLIC_READ``. Mounted at ``/api/killboard/`` from
``config.urls`` via :mod:`apps.killboard.api.urls`.
"""
