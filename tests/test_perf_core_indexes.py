"""Cost-reduction regressions for the shared ESI/cache and provenance-index layers.

Three unrelated-looking findings share one shape: work whose price grows with the size
of a table (or of the Redis keyspace) while the answer it produces stays a single value.

* **P12** — the ESI client wrote a 24h cache entry holding an entire killmail body for
  every killmail ever ingested, on a code path that can never read it back.
* **P13** — the P4 payment reconcile looked payments up by ``context_id`` against an
  unindexed corp wallet journal.
* **P14** — the director dashboard's integration-health panel sorted whole tables by
  ``as_of``/``fetched_at`` to read one timestamp off the top.

The P13/P14 tests assert the *plan*, not a wall-clock number: with sequential scans
priced out of the way, Postgres must be able to answer each query from the new index
**without a Sort node**. That is the real claim — the cost is now O(index depth), not
O(rows) — and it is exactly what an index in the wrong column order would fail. Each
group is paired with an assertion that the answer itself is unchanged, so a future
refactor cannot trade correctness for a prettier plan.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
import responses
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from apps.admin_audit.health import feed_health
from apps.characters.models import CharacterSkillSnapshot
from apps.corporation.models import CorpWalletJournalEntry
from apps.killboard.models import Killmail
from apps.market.models import MarketPrice
from apps.procurement import payments
from apps.procurement.models import (
    ProcurementConfig,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)
from apps.sso.models import EveCharacter
from core.esi.client import ESIClient

pytestmark = pytest.mark.django_db

S = PurchaseOrder.Status


# --- P12: the ESI ETag layer -------------------------------------------------------

_KM_BODY = {
    "killmail_id": 101,
    "killmail_time": "2026-07-30T12:00:00Z",
    "solar_system_id": 30000142,
    "victim": {"character_id": 90001, "corporation_id": 98000001, "ship_type_id": 587},
    "attackers": [{"character_id": 90002, "corporation_id": 98000002}],
}


@responses.activate
def test_killmail_fetch_returns_identical_data_without_caching_the_body():
    """A killmail path must answer exactly as before while leaving no cache entry.

    ``ingest_killmail`` short-circuits on an already-ingested killmail_id, so this path
    is requested once per killmail and never again: the old cache write was a pure
    deposit into a budget nobody could withdraw from.
    """
    responses.add(
        responses.GET,
        "https://esi.evetech.net/killmails/101/abc123/",
        json=_KM_BODY,
        status=200,
        headers={"ETag": "km-etag-101"},
    )
    resp = ESIClient().get("/killmails/101/abc123/", essential=True)

    assert resp.status == 200
    assert resp.not_modified is False
    assert resp.data == _KM_BODY  # the caller sees the same body it always saw
    assert cache.get("esi:etag:/killmails/101/abc123/") is None
    assert "If-None-Match" not in responses.calls[0].request.headers


@responses.activate
def test_killmail_cache_footprint_stays_zero_as_ingest_volume_grows():
    """The bound that matters: entries written is 0, not "fewer", for any N.

    Asserted at two very different N so the claim is independence from ingest volume
    rather than a smaller constant. Against the old client each fetch deposited one
    multi-kilobyte entry with a 24h TTL, so 5 and 25 differed by 20 entries.
    """
    def footprint(count: int) -> int:
        cache.clear()
        paths = [f"/killmails/{i}/hash{i}/" for i in range(count)]
        for path in paths:
            responses.add(
                responses.GET,
                f"https://esi.evetech.net{path}",
                json=_KM_BODY,
                status=200,
                headers={"ETag": "shared-etag"},
            )
        for path in paths:
            assert ESIClient().get(path, essential=True).data == _KM_BODY
        return sum(1 for p in paths if cache.get(ESIClient._etag_key(p)) is not None)

    assert footprint(5) == 0
    assert footprint(25) == 0


@responses.activate
def test_repeatedly_fetched_paths_keep_their_conditional_requests():
    """Guard against over-deleting: the ETag layer still works where it pays off.

    Skills, roster, assets and structures are re-fetched on a beat, so their cached
    bodies really do turn into 304s and saved bandwidth. Only single-fetch paths opted
    out; if someone widened that exclusion this test fails.
    """
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/90001/skills/",
        json={"total_sp": 5_000_000},
        status=200,
        headers={"ETag": "skills-1"},
    )
    first = ESIClient().get("/characters/90001/skills/")
    assert first.data == {"total_sp": 5_000_000}
    assert cache.get("esi:etag:/characters/90001/skills/")["etag"] == "skills-1"

    responses.replace(
        responses.GET,
        "https://esi.evetech.net/characters/90001/skills/",
        status=304,
        headers={"ETag": "skills-1"},
    )
    second = ESIClient().get("/characters/90001/skills/")
    assert second.not_modified is True
    assert second.data == {"total_sp": 5_000_000}
    assert responses.calls[1].request.headers["If-None-Match"] == "skills-1"


# --- plan helper -------------------------------------------------------------------

def _plan(queryset) -> str:
    """EXPLAIN ``queryset`` with sequential scans priced out of contention.

    A test fixture holds a handful of rows, so Postgres would sensibly seq-scan
    everything no matter which indexes exist — which would make an index test pass
    vacuously. Discouraging seq scans instead asks the planner the question we actually
    care about: *given the choice, can this query be answered from an index, in order?*
    If the index is missing or its columns are in the wrong order the planner still
    falls back to a Seq Scan and/or bolts on a Sort, and the assertions below fail.

    ``SET LOCAL`` scopes the setting to the test's own transaction.
    """
    compiler = queryset.query.get_compiler(using=connection.alias)
    sql, params = compiler.as_sql()
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        return "\n".join(row[0] for row in cursor.fetchall())


# --- P13: CorpWalletJournalEntry.context_id ----------------------------------------

def _journal(entry_id, *, context_id, amount="-1000000000",
             ref="contract_price_payment_corp", offset_min=1):
    return CorpWalletJournalEntry.objects.create(
        entry_id=entry_id, division=1,
        date=timezone.now() - timedelta(minutes=offset_min),
        ref_type=ref, amount=Decimal(amount), context_id=context_id,
    )


def test_context_id_payment_lookup_is_an_ordered_index_scan():
    """The reconcile's lookup must seek on context_id and read date already ordered."""
    qs = (
        CorpWalletJournalEntry.objects.filter(
            ref_type__in=["contract_price_payment_corp"],
            context_id=42,
            amount__lt=0,
            date__gte=timezone.now() - timedelta(days=1),
        ).order_by("date")[:1]
    )
    plan = _plan(qs)
    assert "cwje_context_date_idx" in plan, plan
    # No Sort node: date trails the equality column, so the LIMIT 1 stops at the first
    # index row instead of materialising and sorting every match.
    assert "Sort" not in plan, plan


def test_reconcile_still_settles_the_oldest_matching_payment():
    """Same evidence rules, same winner — the index must not change who gets paid."""
    cfg = ProcurementConfig.active()
    cfg.reconcile_enabled = True
    cfg.reconcile_ref_types = ["contract_price_payment_corp"]
    cfg.save()

    supplier = Supplier.objects.create(
        kind=Supplier.Kind.PILOT, entity_id=1001, display_name="S"
    )
    po = PurchaseOrder.objects.create(
        supplier=supplier, status=S.DELIVERED, expected_total_isk=Decimal("1000000000"),
        approved_at=timezone.now() - timedelta(hours=4), contract_id=42,
    )
    PurchaseOrderLine.objects.create(
        po=po, type_id=587, quantity_ordered=10, quantity_received=10,
        unit_price_isk=Decimal("100000000"),
    )
    po.contract_matched_at = timezone.now() - timedelta(hours=3)
    po.save(update_fields=["contract_matched_at"])

    # Two valid payments plus four decoys the predicate must keep rejecting.
    _journal(1, context_id=42, offset_min=90)   # oldest valid -> the expected winner
    _journal(2, context_id=42, offset_min=30)   # valid but newer
    _journal(3, context_id=99, offset_min=120)  # another contract
    _journal(4, context_id=42, offset_min=120, ref="player_donation")  # untrusted type
    _journal(5, context_id=42, offset_min=100, amount="1000000000")  # money in, not out
    _journal(6, context_id=42, offset_min=400)  # predates contract_matched_at

    assert payments.reconcile_payments() == {"status": "ok", "settled": 1}
    po.refresh_from_db()
    assert po.paid_entry_id == 1
    assert po.paid_amount_isk == Decimal("1000000000")


# --- P14: provenance freshness probes ----------------------------------------------

def test_killmail_freshness_probes_avoid_scanning_the_table():
    """Both killmail freshness probes must be single-row index reads.

    The ``fetched_at`` probe runs first; nothing in ingestion writes that column, so the
    ``as_of`` fallback is the branch that really executes on every recompute. Both are
    asserted because the panel's behaviour depends on both.
    """
    fetched_plan = _plan(
        Killmail.objects.exclude(fetched_at=None).order_by("-fetched_at")
        .values_list("fetched_at", flat=True)[:1]
    )
    assert "km_fetched_at_idx" in fetched_plan, fetched_plan
    assert "Sort" not in fetched_plan, fetched_plan

    as_of_plan = _plan(
        Killmail.objects.order_by("-as_of").values_list("as_of", flat=True)[:1]
    )
    assert "km_as_of_idx" in as_of_plan, as_of_plan
    assert "Sort" not in as_of_plan, as_of_plan


def test_market_price_freshness_probe_avoids_scanning_the_table():
    plan = _plan(MarketPrice.objects.order_by("-as_of").values_list("as_of", flat=True)[:1])
    assert "mktprice_as_of_idx" in plan, plan
    assert "Sort" not in plan, plan


def test_latest_skill_snapshot_freshness_probe_avoids_scanning_the_history():
    """The partial index must cover the is_latest predicate, not just the ordering."""
    plan = _plan(
        CharacterSkillSnapshot.objects.filter(is_latest=True)
        .order_by("-as_of").values_list("as_of", flat=True)[:1]
    )
    assert "skillsnap_latest_as_of_idx" in plan, plan
    assert "Sort" not in plan, plan


def test_feed_health_reports_the_same_freshness_values():
    """The indexed probes must still pick the same rows the sorts used to pick."""
    now = timezone.now()
    newest_km_as_of = now - timedelta(minutes=5)
    for offset, as_of in ((3, now - timedelta(hours=9)), (2, newest_km_as_of),
                          (1, now - timedelta(hours=2))):
        Killmail.objects.create(
            killmail_id=1000 + offset, killmail_hash=f"h{offset}",
            killmail_time=now - timedelta(hours=offset), solar_system_id=30000142,
            victim_ship_type_id=587, as_of=as_of,
        )

    newest_price_as_of = now - timedelta(hours=1)
    MarketPrice.objects.create(type_id=34, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("5"), as_of=now - timedelta(days=2))
    MarketPrice.objects.create(type_id=35, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("6"), as_of=newest_price_as_of)

    character = EveCharacter.objects.create(character_id=90001, name="Pilot One")
    newest_skill_as_of = now - timedelta(hours=3)
    # A superseded snapshot that is *newer* than the current one: the probe filters on
    # is_latest before ordering, so it must not win.
    CharacterSkillSnapshot.objects.create(character=character, is_latest=False,
                                          as_of=now - timedelta(minutes=1))
    CharacterSkillSnapshot.objects.create(character=character, is_latest=True,
                                          as_of=newest_skill_as_of)

    feeds = {f["key"]: f for f in feed_health()}
    assert feeds["killmails"]["last"] == newest_km_as_of
    assert feeds["killmails"]["count"] == 3
    assert feeds["market_prices"]["last"] == newest_price_as_of
    assert feeds["skills"]["last"] == newest_skill_as_of
    assert feeds["skills"]["count"] == 1
