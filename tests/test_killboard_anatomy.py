"""KB-22 — killmail detail anatomy: damage bars, parties, SRP chip, comments, value badge."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.killboard import anatomy
from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import KillmailComment
from apps.market.models import MarketPrice

HOME = 98000001


def _seed_prices(prices: dict[int, int]) -> None:
    for type_id, sell_min in prices.items():
        MarketPrice.objects.create(
            type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
            sell_min=Decimal(sell_min),
        )


def _body(kid: int = 100001, victim_corp: int = 99) -> dict:
    """A kill (victim corp 99 by default) with three attackers across two corps; the
    home-corp pilot flies a Rifter (587) for the doctrine-hull badge."""
    return {
        "killmail_id": kid,
        "killmail_time": "2026-06-20T12:00:00Z",
        "solar_system_id": 30002053,
        "victim": {
            "character_id": 2001, "corporation_id": victim_corp,
            "ship_type_id": 587, "damage_taken": 1000,
            "items": [{"item_type_id": 484, "flag": 27, "quantity_destroyed": 1}],
        },
        "attackers": [
            {"character_id": 3001, "corporation_id": 99, "ship_type_id": 587,
             "final_blow": True, "damage_done": 600},
            {"character_id": 3002, "corporation_id": 99, "ship_type_id": 600,
             "damage_done": 300},
            {"character_id": 3003, "corporation_id": HOME, "ship_type_id": 587,
             "damage_done": 100},
        ],
    }


def _active_doctrine_hull(ship_type_id: int = 587):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Doc", category=cat, priority=90,
                                status=Doctrine.Status.ACTIVE)
    return DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=ship_type_id, modules=[])


def _member(django_user_model, username, cid, role):
    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role

    u = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=u, name=username,
                                is_main=True, is_corp_member=True)
    return u


# --- value_tier ---------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (None, None),
    (Decimal("0"), None),
    (Decimal("999999999"), None),
    (Decimal("1000000000"), "1B+"),
    (Decimal("9999999999"), "1B+"),
    (Decimal("10000000000"), "10B+"),
    (Decimal("100000000000"), "100B+"),
])
def test_value_tier(value, expected):
    result = anatomy.value_tier(value)
    assert (str(result) if result is not None else None) == expected


# --- attacker_breakdown (pure) ------------------------------------------------
def test_attacker_breakdown_damage_top_parties_and_doctrine():
    km = SimpleNamespace(damage_taken=1000)
    attackers = [
        SimpleNamespace(character_id=1, corporation_id=99, alliance_id=None,
                        ship_type_id=587, weapon_type_id=484, damage_done=600, final_blow=True),
        SimpleNamespace(character_id=2, corporation_id=99, alliance_id=None,
                        ship_type_id=600, weapon_type_id=600, damage_done=300, final_blow=False),
        SimpleNamespace(character_id=3, corporation_id=HOME, alliance_id=1,
                        ship_type_id=587, weapon_type_id=484, damage_done=100, final_blow=False),
    ]
    out = anatomy.attacker_breakdown(km, attackers, home_corp_id=HOME, hull_ids={587})
    rows = out["rows"]

    assert rows[0]["is_top"] is True and rows[0]["damage_pct"] == 60.0
    assert rows[1]["is_top"] is False and rows[1]["damage_pct"] == 30.0
    # doctrine-hull badge only for the home-corp pilot on a doctrine hull
    assert rows[2]["doctrine_hull"] is True
    assert rows[0]["doctrine_hull"] is False  # corp 99, not home

    parties = out["parties"]
    assert parties[0]["corporation_id"] == 99  # ranked by damage
    assert parties[0]["pilots"] == 2 and parties[0]["damage_pct"] == 90.0
    assert dict(parties[0]["top_ships"]) == {587: 1, 600: 1}
    assert parties[1]["corporation_id"] == HOME and parties[1]["pilots"] == 1


def test_attacker_breakdown_zero_damage_no_divide():
    km = SimpleNamespace(damage_taken=0)
    attackers = [SimpleNamespace(character_id=1, corporation_id=99, alliance_id=None,
                                 ship_type_id=587, weapon_type_id=None, damage_done=0, final_blow=True)]
    out = anatomy.attacker_breakdown(km, attackers, home_corp_id=HOME, hull_ids=set())
    assert out["rows"][0]["damage_pct"] == 0
    assert out["rows"][0]["is_top"] is False  # no damage -> not flagged top


# --- doctrine_hull_ids --------------------------------------------------------
@pytest.mark.django_db
def test_doctrine_hull_ids_active_only(sde):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    active = Doctrine.objects.create(name="A", category=cat, priority=90, status=Doctrine.Status.ACTIVE)
    retired = Doctrine.objects.create(name="R", category=cat, priority=80, status=Doctrine.Status.RETIRED)
    DoctrineFit.objects.create(doctrine=active, name="a", ship_type_id=587, modules=[])
    DoctrineFit.objects.create(doctrine=retired, name="r", ship_type_id=600, modules=[])

    assert anatomy.doctrine_hull_ids() == {587}


# --- related_killmails --------------------------------------------------------
@pytest.mark.django_db
def test_related_killmails_same_battle(sde):
    from apps.killboard.models import BattleReport

    k1 = ingest_killmail(100001, "h1", body=_body(100001))
    k2 = ingest_killmail(100002, "h2", body=_body(100002))
    k3 = ingest_killmail(100003, "h3", body=_body(100003))

    assert anatomy.related_killmails(k1) == []  # not in any battle

    br = BattleReport.objects.create(
        title="Fight", system_ids=[30002053],
        start_time=timezone.now(), end_time=timezone.now(),
    )
    br.killmails.add(k1, k2, k3)

    rel_ids = {k.killmail_id for k in anatomy.related_killmails(k1)}
    assert rel_ids == {100002, 100003}  # excludes self


# --- detail page --------------------------------------------------------------
@pytest.mark.django_db
def test_detail_page_renders_anatomy(client, sde):
    _seed_prices({587: 2000000000})  # 2B hull -> value badge fires
    _active_doctrine_hull(587)
    ingest_killmail(100001, "h1", body=_body(100001))
    html = client.get("/killboard/100001/").content

    assert b"By corporation" in html         # parties panel
    assert b"1B+" in html                    # value badge (>= 1B)
    assert b"width:" in html                  # a damage bar
    assert b"60.0%" in html                   # top attacker damage share


# --- SRP chip privacy ---------------------------------------------------------
@pytest.mark.django_db
def test_srp_chip_owner_officer_only(client, django_user_model, sde):
    from apps.srp.models import SrpClaim

    # A loss for the home corp, victim character 2001.
    ingest_killmail(100001, "h1", body=_body(100001, victim_corp=HOME))
    owner = _member(django_user_model, "owner", 2001, "member")
    SrpClaim.objects.create(killmail_id=100001, claimant=owner,
                            status=SrpClaim.Status.SUBMITTED)
    url = "/killboard/100001/"
    marker = b"SRP pending"

    assert marker not in client.get(url).content  # anonymous: hidden

    client.force_login(owner)
    assert marker in client.get(url).content       # owner: visible

    client.force_login(_member(django_user_model, "peer", 3001, "member"))
    assert marker not in client.get(url).content   # peer member: hidden

    client.force_login(_member(django_user_model, "officer", 4001, "officer"))
    assert marker in client.get(url).content        # officer: visible


# --- comments -----------------------------------------------------------------
@pytest.mark.django_db
def test_comments_post_and_moderation(client, django_user_model, sde):
    ingest_killmail(100001, "h1", body=_body(100001))
    url = "/killboard/100001/"
    post_url = "/killboard/100001/comment/"

    # Anonymous: section hidden, cannot post.
    assert b"No comments yet." not in client.get(url).content
    assert client.post(post_url, {"body": "sneaky"}).status_code == 302
    assert KillmailComment.objects.count() == 0

    # Member posts.
    author = _member(django_user_model, "author", 2001, "member")
    client.force_login(author)
    assert b"No comments yet." in client.get(url).content
    client.post(post_url, {"body": "gf wp"})
    comment = KillmailComment.objects.get()
    assert comment.body == "gf wp" and comment.author_id == author.id
    assert b"gf wp" in client.get(url).content

    # A peer member cannot delete someone else's comment.
    del_url = f"/killboard/100001/comment/{comment.id}/delete/"
    client.force_login(_member(django_user_model, "peer", 3001, "member"))
    client.post(del_url)
    assert KillmailComment.objects.filter(id=comment.id).exists()

    # The author can delete their own.
    client.force_login(author)
    client.post(del_url)
    assert not KillmailComment.objects.filter(id=comment.id).exists()

    # An officer can delete anyone's.
    client.force_login(author)
    client.post(post_url, {"body": "second"})
    c2 = KillmailComment.objects.get()
    client.force_login(_member(django_user_model, "off", 4001, "officer"))
    client.post(f"/killboard/100001/comment/{c2.id}/delete/")
    assert KillmailComment.objects.count() == 0


@pytest.mark.django_db
def test_comment_rejects_empty(client, django_user_model, sde):
    ingest_killmail(100001, "h1", body=_body(100001))
    client.force_login(_member(django_user_model, "m", 2001, "member"))
    client.post("/killboard/100001/comment/", {"body": "   "})
    assert KillmailComment.objects.count() == 0
