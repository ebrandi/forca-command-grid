"""Built-in readiness dimensions (providers).

Importing this package self-registers every provider (order matters — it sets the
dimensions/gaps order the v1 dashboard expects: doctrine, skill, stock, logistics).
``ReadinessConfig.ready()`` imports it at startup so discovery is automatic; adding
a dimension means dropping a ``<key>.py`` here that calls ``register`` — no pipeline
or dashboard edit (doc 05 §6).
"""
from . import (  # noqa: F401  (import = self-register)
    activity,
    doctrine,
    financial,
    fleet_comp,
    infrastructure,
    leadership,
    logistics,
    recruitment,
    srp,
    staging,
    stock,
    strategic,
    support,
)
