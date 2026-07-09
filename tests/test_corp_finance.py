"""Corp wallet sync, member ISK ledger, and the director finance page."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.corporation.finance import member_isk_ledger
from apps.corporation.models import CorpWalletDivision, CorpWalletJournalEntry, EveName
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, balances, journals):
        self.balances = balances
        self.journals = journals

    def get(self, path, token=None):
        if path.endswith("/wallets/"):
            return _Resp(self.balances)
        if "/journal/" in path:
            div = int(path.split("/wallets/")[1].split("/")[0])
            return _Resp(self.journals.get(div, []))
        return _Resp([])


def _user(django_user_model, cid, role):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_sync_stores_balances_and_journal(monkeypatch):
    from apps.corporation import finance as F

    balances = [{"division": 1, "balance": 1000000.50}, {"division": 2, "balance": 500.0}]
    now = timezone.now().isoformat()
    journals = {1: [{"id": 10, "date": now, "ref_type": "player_donation", "amount": 1000.0,
                     "balance": 1000000.5, "first_party_id": 99, "second_party_id": 98,
                     "description": "gift"}], 2: []}
    monkeypatch.setattr(F, "_token_character", lambda corp_id: type("C", (), {"character_id": 1})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")

    res = F.sync_corp_wallets(corp_id=1, client=_Client(balances, journals))
    assert res["status"] == "ok" and res["entries"] == 1
    assert CorpWalletDivision.objects.get(division=1).balance == Decimal("1000000.50")
    assert CorpWalletJournalEntry.objects.get(entry_id=10).amount == Decimal("1000.00")

    # Idempotent: the same journal id is not re-inserted.
    F.sync_corp_wallets(corp_id=1, client=_Client(balances, journals))
    assert CorpWalletJournalEntry.objects.count() == 1


@pytest.mark.django_db
def test_member_isk_ledger_credits_the_member_not_the_npc(django_user_model):
    """Ratting income is credited to the member (second party), never the NPC payer."""
    from apps.corporation.models import CorpMember
    from apps.sso.models import EveCharacter

    # Two corp members; an NPC payer (CONCORD-like) and an outsider donor.
    EveCharacter.objects.create(character_id=90001, name="Ratter Rick", is_corp_member=True)
    CorpMember.objects.create(character_id=90002, name="Miner Mary", corporation_id=98000001)
    EveName.objects.create(entity_id=90003, name="Outside Donor", category="character")

    now = timezone.now()
    # Ratting: NPC pays (first party 1000125 = CONCORD), member earns (second party).
    CorpWalletJournalEntry.objects.create(entry_id=1, division=1, date=now, ref_type="bounty_prizes",
                                          amount=Decimal("700000000"), first_party_id=1000125,
                                          second_party_id=90001)
    CorpWalletJournalEntry.objects.create(entry_id=2, division=1, date=now, ref_type="ess_escrow_transfer",
                                          amount=Decimal("400000000"), first_party_id=1000132,
                                          second_party_id=90002)
    # A donation from an outsider (not a member) is ignored by the *member* ledger.
    CorpWalletJournalEntry.objects.create(entry_id=3, division=1, date=now, ref_type="player_donation",
                                          amount=Decimal("999000000"), first_party_id=90003,
                                          second_party_id=98000001)

    lb = member_isk_ledger()
    by_name = {r["name"]: r for r in lb}
    # The NPC payers (#1000125 CONCORD, #1000132 ESS) and the outsider are absent.
    assert "Ratter Rick" in by_name and "Miner Mary" in by_name
    assert by_name["Ratter Rick"]["total"] == Decimal("700000000")
    assert not any(r["party_id"] in (1000125, 1000132, 90003) for r in lb)


@pytest.mark.django_db
def test_member_isk_ledger_credits_a_member_donation():
    """A donation *from* a corp member counts toward that member."""
    from apps.sso.models import EveCharacter

    EveCharacter.objects.create(character_id=90010, name="Generous Gary", is_corp_member=True)
    CorpWalletJournalEntry.objects.create(entry_id=1, division=1, date=timezone.now(),
                                          amount=Decimal("5000"), first_party_id=90010,
                                          second_party_id=98000001, ref_type="player_donation")
    CorpWalletJournalEntry.objects.create(entry_id=2, division=1, date=timezone.now(),
                                          amount=Decimal("-100"), first_party_id=90010)  # outflow ignored
    lb = member_isk_ledger()
    assert lb and lb[0]["name"] == "Generous Gary" and lb[0]["total"] == Decimal("5000")


@pytest.mark.django_db
def test_finance_view_director_only(client, django_user_model):
    CorpWalletDivision.objects.create(division=1, balance=Decimal("123456789"))
    director = _user(django_user_model, "fin1", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    assert client.get("/roster/finance/").status_code == 200

    client.logout()
    officer = _user(django_user_model, "fin2", rbac.ROLE_OFFICER)
    client.force_login(officer)
    assert client.get("/roster/finance/").status_code == 403  # director-gated
