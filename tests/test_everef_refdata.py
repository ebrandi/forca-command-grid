"""EVE Ref reference-data packaged volumes + SDE celestials grouping."""
from __future__ import annotations

import io
import json
import tarfile

import pytest

from apps.sde.everef_refdata import iter_type_volumes


def _refdata_archive(types: dict) -> io.BytesIO:
    """Build a reference-data-*.tar.xz with a types.json member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        data = json.dumps(types).encode()
        info = tarfile.TarInfo(name="types.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_iter_type_volumes():
    arch = _refdata_archive({
        "16227": {"packaged_volume": 15000, "volume": 252000},
        "34": {"packaged_volume": 0.01},
        "99": {"name": {"en": "no volume here"}},   # skipped — no packaged_volume
    })
    got = dict(iter_type_volumes(arch))
    assert got[16227] == 15000.0 and got[34] == 0.01
    assert 99 not in got


@pytest.mark.django_db
def test_celestials_grouping():
    from apps.navigation.system_info import celestials
    from apps.sde.models import SdeCelestial, SdeRegion, SdeSolarSystem

    SdeRegion.objects.create(region_id=10000002, name="The Forge")
    SdeSolarSystem.objects.create(system_id=30000142, region_id=10000002, name="Jita", security=0.9)
    SdeCelestial.objects.create(item_id=1, system_id=30000142, kind=SdeCelestial.Kind.PLANET,
                                name="Jita I", celestial_index=1)
    SdeCelestial.objects.create(item_id=2, system_id=30000142, kind=SdeCelestial.Kind.MOON,
                                name="Jita I - Moon 1", parent_planet_id=1)
    SdeCelestial.objects.create(item_id=3, system_id=30000142, kind=SdeCelestial.Kind.BELT,
                                name="Jita I - Asteroid Belt 1", parent_planet_id=1)

    c = celestials(30000142)
    assert c["planet_count"] == 1 and c["moon_count"] == 1 and c["belt_count"] == 1
    assert c["planets"][0]["name"] == "Jita I"
    assert c["planets"][0]["moons"] == ["Jita I - Moon 1"]
    assert c["planets"][0]["belts"] == ["Jita I - Asteroid Belt 1"]
    assert celestials(99999) is None  # not loaded → caller falls back to ESI counts
