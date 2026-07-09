"""Hall of Fame scoreboard: ledger + PVP + PVE attribution and ranking.

Ranks pilots (characters) corp-wide — by corporation, not just app users — so the
whole corp shows up, with the contribution ledger folded onto the member's main.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.corporation.models import CorpWalletJournalEntry, EveName
from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail, KillmailParticipant
from apps.pilots.models import ContributionWeights
from apps.pilots.services import record_contribution
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

WHEN = datetime(2026, 5, 15, 12, 0)
HOME = 98000001


def _member(django_user_model, name="eve:1001", char_id=1001):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=char_id, user=user, name=f"Pilot{char_id}",
                                is_main=True, is_corp_member=True)
    return user


def _weights():
    return ContributionWeights.objects.create(
        name="t", is_active=True, enabled=True, task_points=5,
        pvp_points_per_kill=10, pvp_final_blow_bonus=0,
        pve_points_per_mil=Decimal("0.100"), pve_ref_types="bounty_prizes",
    )


def _kill_by(char_id, corporation_id=HOME, final_blow=True, km_id=5001):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=timezone.make_aware(WHEN),
        solar_system_id=30000142, victim_ship_type_id=587,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.ATTACKER,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=0, character_id=char_id,
        corporation_id=corporation_id, final_blow=final_blow,
    )
    return km


@pytest.mark.django_db
def test_scoreboard_combines_ledger_pvp_and_pve(settings, django_user_model):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()
    user = _member(django_user_model)
    record_contribution(user, "task", 1, "tasks", ref_type="t", ref_id="1",
                        occurred_at=timezone.make_aware(WHEN))           # 5 pts, on main
    _kill_by(1001)                                                       # 1 kill + FB → 10 pts
    CorpWalletJournalEntry.objects.create(
        entry_id=1, division=1, date=timezone.make_aware(WHEN), ref_type="bounty_prizes",
        amount=Decimal("50000000"), first_party_id=1000125,  # CONCORD pays
        second_party_id=1001)                                # member earns → 0.1×50 → 5 pts

    from apps.pilots.halloffame import scoreboard

    board = scoreboard(2026, 5)
    top = board["overall"][0]
    assert top["character_id"] == 1001 and top["name"] == "Pilot1001"
    assert top["points"] == 20
    assert {"task", "pvp", "pve"} <= {c["key"] for c in board["categories"]}


@pytest.mark.django_db
def test_pvp_counts_unregistered_corp_pilots(settings, django_user_model):
    # A corp pilot with NO app account still ranks for their kills, named via EveName.
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()
    EveName.objects.create(entity_id=2002, category="character", name="Enemy Slayer")
    _kill_by(2002, km_id=7001)

    from apps.pilots.halloffame import scoreboard

    board = scoreboard(2026, 5)
    names = {p["name"] for p in board["overall"]}
    assert "Enemy Slayer" in names


@pytest.mark.django_db
def test_pvp_excludes_other_corporations(settings, django_user_model):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()
    _kill_by(3003, corporation_id=99999999, km_id=8001)  # not our corp

    from apps.pilots.halloffame import scoreboard

    assert scoreboard(2026, 5)["scored"] is False


@pytest.mark.django_db
def test_pve_respects_configured_ref_types(settings, django_user_model):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()  # ref_types = "bounty_prizes"
    CorpWalletJournalEntry.objects.create(
        entry_id=2, division=1, date=timezone.make_aware(WHEN), ref_type="player_donation",
        amount=Decimal("99000000"), first_party_id=1000125, second_party_id=1001)

    from apps.pilots.halloffame import scoreboard

    assert scoreboard(2026, 5)["scored"] is False


@pytest.mark.django_db
def test_hall_of_fame_page_renders(settings, client, django_user_model):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()
    user = _member(django_user_model)
    record_contribution(user, "task", 1, "tasks", ref_type="t", ref_id="1",
                        occurred_at=timezone.make_aware(WHEN))
    client.force_login(user)
    html = client.get("/pilots/hall-of-fame/?m=2026-05").content.decode()
    assert "Hall of Fame" in html and "Pilot1001" in html


@pytest.mark.django_db
def test_mining_scored_from_the_ledger(settings, django_user_model):
    # Mining comes straight from the corp mining ledger (observer data), valued at
    # Jita — no payout needed — so an unregistered belt miner still ranks.
    from apps.market.models import MarketPrice
    from apps.mining.models import MiningLedgerEntry, MiningObserver

    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()  # mining_points_per_mil defaults to 0.05
    MarketPrice.objects.create(type_id=18, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("10"))
    obs = MiningObserver.objects.create(observer_id=1)
    MiningLedgerEntry.objects.create(observer=obs, character_id=4004, character_name="Belt Miner",
                                     type_id=18, quantity=5_000_000, day=WHEN.date())  # 50M ISK

    from apps.pilots.halloffame import scoreboard

    board = scoreboard(2026, 5)
    mining = next(c for c in board["categories"] if c["key"] == "mining")
    assert mining["rows"] and mining["rows"][0]["name"] == "Belt Miner"


@pytest.mark.django_db
def test_all_categories_shown_even_when_empty(settings, django_user_model):
    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()
    from apps.pilots.halloffame import CATEGORIES, scoreboard

    board = scoreboard(2026, 5)
    keys = {c["key"] for c in board["categories"]}
    assert keys == {k for k, _ in CATEGORIES}          # every category present
    assert all(c["how"] for c in board["categories"])  # each shows how it scores


@pytest.mark.django_db
def test_opted_out_pilot_excluded_from_hall_of_fame(settings, django_user_model):
    """PCC-1: a pilot with public_recognition=False appears on NO Hall of Fame
    category (ledger/pvp/pve), and neither do their alts — not just the feed."""
    from apps.pilots.halloffame import scoreboard
    from apps.pilots.models import PilotPreference

    settings.FORCA_HOME_CORP_ID = HOME
    cache.clear()
    _weights()

    # An opted-OUT pilot with activity across ledger + pvp + pve, plus an alt kill.
    hidden = _member(django_user_model, name="eve:1001", char_id=1001)
    PilotPreference.objects.create(user=hidden, public_recognition=False)
    record_contribution(hidden, "task", 1, "tasks", ref_type="t", ref_id="1",
                        occurred_at=timezone.make_aware(WHEN))
    _kill_by(1001)
    CorpWalletJournalEntry.objects.create(
        entry_id=1, division=1, date=timezone.make_aware(WHEN), ref_type="bounty_prizes",
        amount=Decimal("50000000"), first_party_id=1000125, second_party_id=1001)
    EveCharacter.objects.create(character_id=1091, user=hidden, name="HiddenAlt",
                                is_corp_member=True)
    _kill_by(1091, km_id=5091)

    # A normal pilot who should still appear.
    shown = _member(django_user_model, name="eve:1002", char_id=1002)
    record_contribution(shown, "task", 1, "tasks", ref_type="t", ref_id="2",
                        occurred_at=timezone.make_aware(WHEN))

    board = scoreboard(2026, 5)
    overall_cids = {p["character_id"] for p in board["overall"]}
    assert 1001 not in overall_cids and 1091 not in overall_cids  # opted out + alt: gone
    assert 1002 in overall_cids                                    # opted in: present
    for cat in board["categories"]:
        assert all(r["character_id"] not in (1001, 1091) for r in cat["rows"])


@pytest.mark.django_db
def test_available_months_includes_current(django_user_model):
    from apps.pilots.halloffame import available_months

    months = available_months()
    now = timezone.now()
    assert months and months[0]["key"] == f"{now.year:04d}-{now.month:02d}"
