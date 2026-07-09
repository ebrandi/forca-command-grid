"""Intelligence-source providers (design doc 04).

Each module registers a ``SourceProvider`` that turns one existing app's services
into one typed slice of the Intelligence Snapshot. Importing this package (from
``AppConfig.ready()``) self-registers them. Adding a source = dropping a module here
and listing it below — no pipeline edit.
"""
from __future__ import annotations

# Source provider modules are imported here so they self-register on app load.
# Adding a source = dropping a module here and listing it below — no pipeline edit.
# (P1 ships these six; manpower/industry/logistics/operations/recruitment/
#  recommendations follow in later phases.)
from . import (  # noqa: F401
    combat,
    doctrine,
    finance,
    infrastructure,
    readiness,
    srp,
)
