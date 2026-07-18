"""FORCA-owned ship-fitting calculation engine (Tocha's Lab).

An independent, server-side implementation derived from authoritative EVE data
and publicly documented dogma mechanics — NOT a port of any external engine.
The engine is deterministic, has no dependency on request/ESI/network state, and
is reached only through :mod:`apps.fitting.engine.adapter` (the FORCA domain
boundary), so the implementation can be replaced without touching the feature.
"""
