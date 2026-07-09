"""resolve_ids must isolate unresolvable ids.

ESI's /universe/names/ returns 404 for the WHOLE batch if any single id is
unresolvable (e.g. a player structure used as a member's location). One bad id
must not block every other name — otherwise unlinked pilots show as raw ids.
"""
from __future__ import annotations

import json

import pytest
import responses

from apps.corporation.models import EveName
from core.esi.names import resolve_ids

NAMES = {
    1001: "Alice",
    1002: "Bob",
    1003: "Carol",
}
BAD_ID = 1_400_000_000_000  # a player structure id /universe/names/ can't resolve


def _callback(request):
    posted = json.loads(request.body)
    if BAD_ID in posted:
        # ESI fails the entire request when any id is unresolvable.
        return (404, {}, json.dumps({"error": "Ensure all ids are valid"}))
    body = [{"id": i, "name": NAMES[i], "category": "character"} for i in posted if i in NAMES]
    return (200, {}, json.dumps(body))


@responses.activate
@pytest.mark.django_db
def test_one_bad_id_does_not_block_the_rest():
    responses.add_callback(
        responses.POST,
        "https://esi.evetech.net/universe/names/",
        callback=_callback,
        content_type="application/json",
    )
    added = resolve_ids([1001, 1002, 1003, BAD_ID])

    # All three real pilots resolve despite the unresolvable structure id.
    stored = dict(EveName.objects.values_list("entity_id", "name"))
    assert stored == {1001: "Alice", 1002: "Bob", 1003: "Carol"}
    assert added == 3


@responses.activate
@pytest.mark.django_db
def test_all_good_ids_resolve_in_one_call():
    responses.add_callback(
        responses.POST,
        "https://esi.evetech.net/universe/names/",
        callback=_callback,
        content_type="application/json",
    )
    resolve_ids([1001, 1002])
    # No bad id → resolved without any split (single POST).
    assert len(responses.calls) == 1
    assert EveName.objects.count() == 2


@responses.activate
@pytest.mark.django_db
def test_already_known_ids_are_not_refetched():
    EveName.objects.create(entity_id=1001, name="Alice", category="character")
    responses.add_callback(
        responses.POST,
        "https://esi.evetech.net/universe/names/",
        callback=_callback,
        content_type="application/json",
    )
    resolve_ids([1001])  # already known → no HTTP call at all
    assert len(responses.calls) == 0
