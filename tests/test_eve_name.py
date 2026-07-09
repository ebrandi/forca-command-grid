"""eve_name must not cache the '#id' fallback (names resolve asynchronously)."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.corporation.models import EveName
from apps.sde.templatetags.eve import eve_name


@pytest.mark.django_db
def test_eve_name_does_not_stick_on_fallback():
    cache.clear()
    # Unknown id → fallback shown, but NOT cached.
    assert eve_name(98530096) == "#98530096"
    # The name is resolved later (background /universe/names/).
    EveName.objects.create(entity_id=98530096, name="Cyno is Up Jump Jump", category="corporation")
    # It must appear immediately — the fallback was not cached.
    assert eve_name(98530096) == "Cyno is Up Jump Jump"


@pytest.mark.django_db
def test_eve_name_caches_real_names():
    cache.clear()
    EveName.objects.create(entity_id=500001, name="Caldari State", category="faction")
    assert eve_name(500001) == "Caldari State"
    # A real name is cached, so a later DB change isn't reflected until TTL.
    EveName.objects.filter(entity_id=500001).update(name="changed")
    assert eve_name(500001) == "Caldari State"


@pytest.mark.django_db
def test_eve_name_empty_id():
    assert eve_name(None) == ""
    assert eve_name(0) == ""
