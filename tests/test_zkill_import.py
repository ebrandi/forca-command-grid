"""The paced full-history zKillboard import command."""
from __future__ import annotations

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_import_zkill_history_pages_dedups_and_limits(monkeypatch):
    from apps.killboard.management.commands import import_zkill_history as mod
    from core.esi import names as names_mod
    from core.esi.adapters import zkill

    # Two full pages (200 each) then a short page → end of history. Page 2 repeats
    # one id from page 1 to prove de-duplication.
    page1 = [(i, f"h{i}") for i in range(1, 201)]
    page2 = [(200, "h200")] + [(i, f"h{i}") for i in range(201, 400)]  # 200 dupes page1
    page3 = [(i, f"h{i}") for i in range(400, 420)]  # short page → stop
    pages = {1: page1, 2: page2, 3: page3}
    monkeypatch.setattr(zkill, "corporation_killmail_refs_page", lambda corp, page: pages.get(page, []))

    ingested: list[int] = []
    monkeypatch.setattr(mod, "ingest_killmail", lambda kid, kh, source, client: ingested.append(kid))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)  # no real waiting in tests
    monkeypatch.setattr(names_mod, "backfill_killmail_names", lambda: 0)
    from apps.killboard import stats as stats_mod
    monkeypatch.setattr(stats_mod, "rebuild_corp_metrics", lambda: 0)

    # --limit caps how many NEW bodies are fetched, after de-dup across pages.
    call_command("import_zkill_history", "--corp", "98028546", "--limit", "5",
                 "--zkill-delay", "0", "--esi-delay", "0")
    assert ingested == [1, 2, 3, 4, 5]  # first 5 unique ids, no duplicate, limit honoured


@pytest.mark.django_db
def test_import_zkill_history_skips_already_stored(monkeypatch):
    from django.utils import timezone

    from apps.killboard.management.commands import import_zkill_history as mod
    from apps.killboard.models import Killmail
    from core.esi import names as names_mod
    from core.esi.adapters import zkill

    # One mail already in the DB — it must not be re-fetched from ESI.
    Killmail.objects.create(killmail_id=10, killmail_time=timezone.now(),
                            solar_system_id=30000142, victim_ship_type_id=587)
    monkeypatch.setattr(zkill, "corporation_killmail_refs_page",
                        lambda corp, page: [(10, "h10"), (11, "h11")] if page == 1 else [])
    fetched: list[int] = []
    monkeypatch.setattr(mod, "ingest_killmail", lambda kid, kh, source, client: fetched.append(kid))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(names_mod, "backfill_killmail_names", lambda: 0)
    from apps.killboard import stats as stats_mod
    monkeypatch.setattr(stats_mod, "rebuild_corp_metrics", lambda: 0)

    call_command("import_zkill_history", "--corp", "98028546", "--zkill-delay", "0", "--esi-delay", "0")
    assert fetched == [11]  # only the new one; the stored id 10 is skipped
