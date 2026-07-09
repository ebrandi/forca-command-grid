"""Combat Intelligence — battle after-action review (facts, AAR generation, retrieval, gating).

Fixture battle: in Tama we lose a Guardian (logi, 250M) and kill a Stabber (80M) — an
unfavorable, logi-losing engagement, which is exactly what an AAR should surface.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.command_intel import battle, retrieval, services
from apps.command_intel.battle_analysis import run_battle_analysis
from apps.command_intel.models import BattleAnalysis, Classification
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP, ENEMY_CORP = 98000001, 98000002
OUR_PILOT, ENEMY_PILOT = 95000001, 97000003
GUARDIAN, STABBER = 11987, 622
TAMA = 30002813


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"ci-bt-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _sde():
    from apps.sde.models import SdeCategory, SdeGroup, SdeRegion, SdeSolarSystem, SdeType

    cat, _ = SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    logi, _ = SdeGroup.objects.get_or_create(group_id=832, defaults={"category": cat, "name": "Logistics"})
    cruiser, _ = SdeGroup.objects.get_or_create(group_id=26, defaults={"category": cat, "name": "Cruiser"})
    SdeType.objects.get_or_create(type_id=GUARDIAN, defaults={"group": logi, "name": "Guardian"})
    SdeType.objects.get_or_create(type_id=STABBER, defaults={"group": cruiser, "name": "Stabber"})
    region, _ = SdeRegion.objects.get_or_create(region_id=10000020, defaults={"name": "Tash-Murkon"})
    SdeSolarSystem.objects.get_or_create(
        system_id=TAMA, defaults={"region": region, "name": "Tama", "security": 0.3}
    )


def _names():
    from apps.corporation.models import EveName

    EveName.objects.get_or_create(entity_id=ENEMY_CORP, defaults={"name": "Enemy Corp", "category": "corporation"})
    EveName.objects.get_or_create(entity_id=OUR_PILOT, defaults={"name": "Our Pilot", "category": "character"})


def _battle():
    from apps.killboard.models import BattleReport, Killmail, KillmailParticipant

    _sde()
    _names()
    t0 = timezone.now() - dt.timedelta(minutes=20)
    t1 = timezone.now() - dt.timedelta(minutes=5)

    km1 = Killmail.objects.create(
        killmail_id=1, killmail_hash="h1", killmail_time=t0, solar_system_id=TAMA,
        victim_ship_type_id=GUARDIAN, victim_corporation_id=HOME_CORP, victim_character_id=OUR_PILOT,
        total_value=250_000_000, involves_home_corp=True, home_corp_role="victim",
    )
    KillmailParticipant.objects.create(killmail=km1, role="victim", seq=0, character_id=OUR_PILOT,
                                       corporation_id=HOME_CORP, ship_type_id=GUARDIAN)
    KillmailParticipant.objects.create(killmail=km1, role="attacker", seq=1, character_id=ENEMY_PILOT,
                                       corporation_id=ENEMY_CORP, ship_type_id=STABBER, final_blow=True,
                                       damage_done=5000)
    km2 = Killmail.objects.create(
        killmail_id=2, killmail_hash="h2", killmail_time=t1, solar_system_id=TAMA,
        victim_ship_type_id=STABBER, victim_corporation_id=ENEMY_CORP,
        total_value=80_000_000, involves_home_corp=True, home_corp_role="attacker",
    )
    KillmailParticipant.objects.create(killmail=km2, role="victim", seq=0, character_id=ENEMY_PILOT,
                                       corporation_id=ENEMY_CORP, ship_type_id=STABBER)
    KillmailParticipant.objects.create(killmail=km2, role="attacker", seq=1, character_id=OUR_PILOT,
                                       corporation_id=HOME_CORP, ship_type_id=GUARDIAN, final_blow=True)
    br = BattleReport.objects.create(
        title="Battle in Tama", system_ids=[TAMA], start_time=t0, end_time=t1,
        sides={"corporations": [{"corporation_id": HOME_CORP, "kills": 1, "losses": 1, "isk_lost": "250000000"}]},
        ship_breakdown={"11987": 1, "622": 1},
    )
    br.killmails.set([1, 2])
    return br


class _FakeResult:
    def __init__(self, obj):
        self.obj = obj
        self.text = obj.get("summary", "")
        self.usage = {"input": 30, "output": 12}
        self.model = "MiniMax-M2.7"
        self.latency_ms = 1500
        self.finish_reason = "stop"


class _FakeClient:
    def __init__(self, obj):
        self._obj = obj

    def generate(self, req):
        return _FakeResult(self._obj)


@pytest.mark.django_db
def test_battle_facts_are_computed_deterministically():
    facts = battle.battle_facts(_battle())
    t = facts["totals"]
    assert t["our_losses"] == 1 and t["our_kills"] == 1
    assert t["isk_lost"] == 250_000_000 and t["isk_destroyed"] == 80_000_000
    assert t["isk_swing"] == -170_000_000
    assert facts["outcome"] == "unfavorable"
    assert t["logi_lost"] == 1                       # the Guardian is a logistics hull
    assert "Tama" in facts["systems"]
    loss = facts["our_losses_detail"][0]
    assert loss["ship"] == "Guardian" and loss["pilot"] == "Our Pilot"
    assert loss["killed_by"] == "Enemy Corp"          # unnamed enemy pilot → their corp name
    assert any(r["ship"] == "Stabber" for r in facts["enemy_composition"])


@pytest.mark.django_db
def test_own_pilot_optout_is_pseudonymized(django_user_model):
    from apps.pilots.models import PilotPreference
    from apps.sso.models import EveCharacter

    u = django_user_model.objects.create(username="opt-pilot")
    PilotPreference.objects.create(user=u, public_recognition=False)
    EveCharacter.objects.create(character_id=OUR_PILOT, user=u, name="Our Pilot",
                                is_main=True, is_corp_member=True)
    facts = battle.battle_facts(_battle())
    assert facts["our_losses_detail"][0]["pilot"].startswith("Pilot #")  # opt-out wins over naming


@pytest.mark.django_db
def test_analysis_degrades_to_facts_when_llm_off(settings):
    settings.COMMAND_INTEL_ENABLED = False
    br = _battle()
    a = BattleAnalysis.objects.create(battle_report_id=br.pk, status=BattleAnalysis.Status.PENDING)
    run_battle_analysis(a)
    a.refresh_from_db()
    assert a.status == BattleAnalysis.Status.READY_DEGRADED
    assert a.facts["totals"]["our_losses"] == 1
    assert a.body["summary"]                          # a facts-only summary, never empty


@pytest.mark.django_db
def test_analysis_ready_with_grounded_narrative(settings, monkeypatch):
    settings.COMMAND_INTEL_ENABLED = True
    br = _battle()
    a = BattleAnalysis.objects.create(battle_report_id=br.pk, status=BattleAnalysis.Status.PENDING)
    obj = {"summary": "Lost a Guardian in Tama.", "what_happened": "Roam gank.",
           "what_went_wrong": ["logi died first"], "what_to_improve": ["pre-position reps"],
           "key_losses": ["Guardian"]}
    monkeypatch.setattr("apps.command_intel.llm.client.LLMClient", lambda *a, **k: _FakeClient(obj))
    run_battle_analysis(a)
    a.refresh_from_db()
    assert a.status == BattleAnalysis.Status.READY
    assert a.body["summary"].startswith("Lost a Guardian")
    assert "logi died first" in a.body["what_went_wrong"]
    assert a.model_name == "MiniMax-M2.7"


@pytest.mark.django_db
def test_retrieval_is_combat_aware(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    br = _battle()
    ids = [h["id"] for h in retrieval.retrieve("Tama battle logistics", officer, k=20)]
    assert f"battle:{br.pk}" in ids                   # /command/ask/ can now ground on battles


@pytest.mark.django_db
def test_combat_passage_carries_windowed_ship_counts(django_user_model, settings):
    # Regression: "how many ships did we lose last 30 days" must ground on a real count,
    # not just ISK — the combat passage now carries the 7d/30d ship-loss rollups.
    from apps.killboard.models import CombatMetric

    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-cm")
    CombatMetric.objects.create(
        entity_type=CombatMetric.EntityType.CORPORATION, entity_id=settings.FORCA_HOME_CORP_ID,
        window="30d", kills=249, losses=68, isk_lost=2_898_309_129, isk_destroyed=61_000_000_000,
    )
    hits = retrieval.retrieve("how many ships did we lose last 30 days", officer, k=25)
    combat = next((h for h in hits if h["id"] == "combat:performance"), None)
    assert combat is not None
    assert "68 ships lost" in combat["text"]
    assert "Last 30d" in combat["text"]


@pytest.mark.django_db
def test_request_battle_analysis_dedupes_in_flight(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-dedupe")
    br = _battle()
    a1 = services.request_battle_analysis(user=officer, battle_report_id=br.pk)
    a2 = services.request_battle_analysis(user=officer, battle_report_id=br.pk)
    assert a1.pk == a2.pk
    assert BattleAnalysis.objects.filter(battle_report_id=br.pk).count() == 1


@pytest.mark.django_db
def test_battles_page_is_officer_gated(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-pg"))
    assert client.get("/command/battles/").status_code == 200
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-pg"))
    assert client.get("/command/battles/").status_code in (403, 302)


@pytest.mark.django_db
def test_battle_generate_creates_analysis(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-gen")
    br = _battle()
    client.force_login(officer)
    resp = client.post(f"/command/battles/{br.pk}/analyze/")
    assert resp.status_code == 302
    assert BattleAnalysis.objects.filter(battle_report_id=br.pk).exists()


@pytest.mark.django_db
def test_friendly_fire_final_blow_is_pseudonymized():
    # A blue-on-blue (awox) killer is one of ours — must not be named as the "killed_by".
    from apps.command_intel import config
    from apps.killboard.models import BattleReport, Killmail, KillmailParticipant

    config.set("battle", {"name_own_pilots": False})
    _sde()
    _names()
    t0 = timezone.now()
    km = Killmail.objects.create(
        killmail_id=5, killmail_hash="h5", killmail_time=t0, solar_system_id=TAMA,
        victim_ship_type_id=GUARDIAN, victim_corporation_id=HOME_CORP, victim_character_id=OUR_PILOT,
        total_value=100, involves_home_corp=True, home_corp_role="victim",
    )
    KillmailParticipant.objects.create(killmail=km, role="attacker", seq=1, character_id=OUR_PILOT,
                                       corporation_id=HOME_CORP, ship_type_id=STABBER, final_blow=True)
    br = BattleReport.objects.create(title="Awox", system_ids=[TAMA], start_time=t0, end_time=t0, sides={})
    br.killmails.set([5])
    facts = battle.battle_facts(br)
    assert facts["our_losses_detail"][0]["killed_by"].startswith("Pilot #")


@pytest.mark.django_db
def test_battles_list_hides_an_above_clearance_aar(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-list")
    br = _battle()
    BattleAnalysis.objects.create(
        battle_report_id=br.pk, classification=Classification.DIRECTOR_EYES_ONLY,
        status=BattleAnalysis.Status.READY, body={"summary": "x"}, facts={"totals": {}},
    )
    client.force_login(officer)
    body = client.get("/command/battles/").content
    assert b"analysed" not in body  # the "▲ analysed" status is hidden from the officer


@pytest.mark.django_db
def test_battle_status_hides_an_aar_above_clearance(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-clr")
    br = _battle()
    a = BattleAnalysis.objects.create(
        battle_report_id=br.pk, classification=Classification.DIRECTOR_EYES_ONLY,
        status=BattleAnalysis.Status.READY, body={"summary": "eyes only"}, facts={"totals": {}},
    )
    client.force_login(officer)
    resp = client.get(f"/command/battles/analysis/{a.pk}/status/", HTTP_HX_REQUEST="true")
    assert resp.status_code == 403  # officer can't see a director-eyes-only AAR
