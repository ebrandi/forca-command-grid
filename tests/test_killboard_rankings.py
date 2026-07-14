"""PvP rankings: leaderboard math, fairness rules, ranks, and the view."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.killboard.leaderboards import (
    combat_rank,
    danger_rating,
    leaderboards,
    pilot_combat_card,
    window_for,
)
from apps.killboard.models import Killmail, KillmailParticipant

HOME = 98000001
ENEMY = 55555
A = 1001  # pilot A
B = 1002  # pilot B


def _kill(km_id, *, value, points, attackers, is_solo=False, is_npc=False,
          home_role, victim_char=None, victim_corp=ENEMY):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587,
        total_value=Decimal(value), points=points, is_solo=is_solo, is_npc=is_npc,
        involves_home_corp=True, home_corp_role=home_role,
        victim_character_id=victim_char, victim_corporation_id=victim_corp,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0,
        character_id=victim_char, corporation_id=victim_corp, ship_type_id=587,
    )
    for i, (char, corp, fb) in enumerate(attackers, start=1):
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=i, character_id=char,
            corporation_id=corp, ship_type_id=587, final_blow=fb, damage_done=100,
        )
    return km


@pytest.fixture
def battle_data(db, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    # A's kills (incl. a solo and several final blows) — enough fights to qualify.
    _kill(1, value="100000000", points=10, is_solo=True,
          attackers=[(A, HOME, True)], home_role=Killmail.HomeRole.ATTACKER)
    _kill(2, value="100000000", points=10,
          attackers=[(A, HOME, False)], home_role=Killmail.HomeRole.ATTACKER)
    # Shared kill: A and B both on the mail, B lands the blow.
    _kill(3, value="100000000", points=10,
          attackers=[(A, HOME, False), (B, HOME, True)], home_role=Killmail.HomeRole.ATTACKER)
    _kill(5, value="100000000", points=10,
          attackers=[(A, HOME, True)], home_role=Killmail.HomeRole.ATTACKER)
    _kill(6, value="100000000", points=10,
          attackers=[(A, HOME, True)], home_role=Killmail.HomeRole.ATTACKER)
    # B's big kill — most valuable of the period.
    _kill(4, value="1000000000", points=50,
          attackers=[(B, HOME, True)], home_role=Killmail.HomeRole.ATTACKER)
    # A's PvP loss (counts) and a ratting death to NPCs (must NOT count).
    _kill(10, value="200000000", points=1, attackers=[(9000, ENEMY, True)],
          home_role=Killmail.HomeRole.VICTIM, victim_char=A, victim_corp=HOME)
    _kill(11, value="50000000", points=1, is_npc=True, attackers=[(0, None, True)],
          home_role=Killmail.HomeRole.VICTIM, victim_char=A, victim_corp=HOME)
    return None


def _board(data, key):
    return next(c["rows"] for c in data["categories"] if c["key"] == key)


@pytest.mark.django_db
def test_top_killers_counts_every_attacker_on_a_mail(battle_data):
    data = leaderboards("all", use_cache=False)
    rows = _board(data, "top_killers")
    assert [(r["character_id"], r["value"]) for r in rows] == [(A, 5), (B, 2)]


@pytest.mark.django_db
def test_isk_and_points_boards(battle_data):
    data = leaderboards("all", use_cache=False)
    isk = _board(data, "isk_destroyed")
    # B's 1.1B beats A's 0.5B; full mail value credited to each attacker.
    assert isk[0]["character_id"] == B
    assert isk[0]["value"] == Decimal("1100000000")
    points = _board(data, "points")
    assert [r["character_id"] for r in points] == [B, A]  # 60 vs 50


@pytest.mark.django_db
def test_final_blows_and_solo(battle_data):
    data = leaderboards("all", use_cache=False)
    fb = {r["character_id"]: r["value"] for r in _board(data, "final_blows")}
    assert fb == {A: 3, B: 2}
    solo = _board(data, "solo_kills")
    assert solo == [{"place": 1, "character_id": A, "value": 1, "secondary": None}]


@pytest.mark.django_db
def test_npc_loss_excluded_from_losses(battle_data):
    data = leaderboards("all", use_cache=False)
    lost = _board(data, "isk_lost")
    # Only the 200M PvP loss; the 50M ratting death is ignored.
    assert lost == [{"place": 1, "character_id": A, "value": Decimal("200000000"),
                     "secondary": "1 losses"}]


@pytest.mark.django_db
def test_efficiency_requires_minimum_fights(battle_data):
    data = leaderboards("all", use_cache=False)
    eff = _board(data, "efficiency")
    # A has 6 fights and qualifies (500M / 700M ≈ 71%); B has only 2 fights.
    assert [r["character_id"] for r in eff] == [A]
    assert round(eff[0]["value"]) == 71


# --- corp combat roster (colleague-facing pilot directory) -------------------
def _corp_char(cid, name):
    from apps.sso.models import EveCharacter

    return EveCharacter.objects.create(
        character_id=cid, name=name, is_corp_member=True, is_main=True,
    )


@pytest.mark.django_db
def test_corp_combat_roster_orders_by_name_and_includes_untested(battle_data):
    from apps.killboard.leaderboards import corp_combat_roster

    _corp_char(A, "Bravo Pilot")     # has kills + a loss
    _corp_char(B, "Alfa Pilot")      # has kills only
    _corp_char(3003, "Zulu Pilot")   # no killmails → Untested
    roster = corp_combat_roster(use_cache=False)
    # Ordered by name, every corp pilot present (even the one with no record).
    assert [r["name"] for r in roster] == ["Alfa Pilot", "Bravo Pilot", "Zulu Pilot"]
    by_id = {r["character_id"]: r for r in roster}
    assert by_id[A]["kills"] == 5 and by_id[A]["losses"] == 1
    assert by_id[A]["rank"]["title"] and by_id[A]["danger"]["label"]
    untested = by_id[3003]
    assert untested["has_record"] is False
    assert untested["kills"] == 0 and untested["danger"]["label"] == "Untested"


@pytest.mark.django_db
def test_roster_page_lists_pilots_with_stats_links(client, django_user_model, battle_data):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    member = django_user_model.objects.create(username="eve:1001")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    _corp_char(A, "Bravo Pilot")
    _corp_char(B, "Alfa Pilot")
    client.force_login(member)
    html = client.get("/killboard/roster/").content.decode()
    assert f"/killboard/pilot/{A}/" in html and f"/killboard/pilot/{B}/" in html
    assert "See combat stats" in html
    assert "Bravo Pilot" in html and "Alfa Pilot" in html


@pytest.mark.django_db
def test_roster_page_denied_for_non_member(client, django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    outsider = django_user_model.objects.create(username="eve:404")
    client.force_login(outsider)
    resp = client.get("/killboard/roster/")
    # A pilot with no corp/alliance standing never reaches the roster: the app
    # either 403s or bounces them to onboarding — never serves the page (200).
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_most_valuable_kill_is_credited_to_the_finisher(battle_data):
    data = leaderboards("all", use_cache=False)
    mvk = data["most_valuable"]
    assert mvk[0]["killmail_id"] == 4
    assert mvk[0]["character_id"] == B
    assert mvk[0]["value"] == Decimal("1000000000")


@pytest.mark.django_db
def test_pilot_combat_card_rank(battle_data):
    card = pilot_combat_card(A, use_cache=False)
    assert card["has_record"] is True
    assert card["kills"] == 5
    assert card["losses"] == 1
    assert card["rank"]["title"] == "Skirmisher"  # 5 kills → 5..9 band (seeded ladder)
    assert card["solo_kills"] == 1


@pytest.mark.django_db
def test_combat_rank_ladder():
    # The ladder is now the DB-driven seeded default (see killboard migration 0009).
    assert combat_rank(0)["title"] == "Dockside Recruit"
    assert combat_rank(5)["title"] == "Skirmisher"
    assert combat_rank(10)["title"] == "Line Pilot"
    assert combat_rank(50)["title"] == "Proven Combatant"
    assert combat_rank(1000)["title"] == "Ace Pilot"


@pytest.mark.django_db
def test_danger_rating_bands():
    # Turn the newbro softening off so the raw Snuggly band is exercised here; the
    # softened "Learning" behaviour is covered in tests/test_killboard_newbro.py.
    from django.core.cache import cache

    from apps.killboard.models import NewbroConfig

    NewbroConfig.objects.create(soften_danger_label=False)
    cache.delete("killboard:newbro_soften")
    assert danger_rating(9, 1)["label"] == "Dangerous"
    assert danger_rating(6, 4)["label"] == "Risky"
    assert danger_rating(1, 9)["label"] == "Snuggly"
    assert danger_rating(0, 0)["label"] == "Untested"


def test_window_keys_resolve():
    assert window_for("month").start is not None
    last = window_for("lastmonth")
    assert last.start is not None and last.end is not None and last.start < last.end
    assert window_for("all").start is None
    assert window_for("garbage").key == "30d"  # safe default


@pytest.mark.django_db
def test_rankings_view_renders(client, battle_data):
    resp = client.get("/killboard/rankings/?window=all")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Combat rankings" in html
    assert "Most valuable kills" in html
    assert "Dockside Recruit" in html  # DB-driven rank ladder legend present


# --- i18n: the month-name trap + language-scoped cache keys (Seam D) ---------
def test_month_names_localise_instead_of_staying_c_locale_english():
    """THE TRAP: ``calendar.month_name`` / ``month_abbr`` come from the C library and stay
    English under every ``translation.override``. Both killboard month renderers now go
    through Django's translated names. March is used because its German name (März) differs
    from English — unlike April/August/September/November, which are spelled the same.
    """
    import calendar

    from django.utils import translation

    from apps.killboard.aggregation import _period_label
    from apps.killboard.analytics import _month_label

    with translation.override("en"):
        assert _period_label(2026, 3) == "March 2026"   # unchanged English output
        assert _month_label(2026, 3) == "Mar 26"        # ditto (was "%b %y")
    with translation.override("de"):
        assert _period_label(2026, 3) == "März 2026"
        assert _month_label(2026, 3) == "Mär 26"
        # The old implementation, for contrast: still stubbornly English under German.
        assert calendar.month_name[3] == "March"
        assert calendar.month_abbr[3] == "Mar"


def test_window_month_label_is_byte_identical_in_english_and_localises():
    """The window label keeps its exact English rendering and picks up the active locale."""
    from django.utils import formats, translation

    now = timezone.now()
    with translation.override("en"):
        english = window_for("month").label
        assert english == f"This month · {now.strftime('%B')}"  # English output unchanged
    with translation.override("de"):
        german = window_for("month").label
        # The month segment is whatever Django renders for this locale — not the C locale's.
        assert german.endswith(formats.date_format(now, "F"))


def test_window_labels_translate_and_key_stays_a_code_value():
    """Only the label is marked — the window KEY is a code value (query string + cache key)."""
    from django.utils import translation

    with translation.override("en"):
        assert window_for("7d").label == "Last 7 days"
    with translation.override("de"):
        w = window_for("7d")
    assert w.key == "7d"  # never translated


@pytest.mark.django_db
def test_leaderboard_cache_is_language_scoped(battle_data):
    """Two readers in different languages must not share one cached rankings payload."""
    from django.core.cache import cache
    from django.utils import translation

    from apps.killboard.leaderboards import CACHE_VERSION

    cache.clear()
    with translation.override("en"):
        leaderboards("all")
    with translation.override("de"):
        leaderboards("all")

    base = f"kb:lb:{CACHE_VERSION}:{HOME}:all"
    assert cache.get(f"{base}:en") is not None
    assert cache.get(f"{base}:de") is not None
    assert cache.get(base) is None, "the unscoped key must no longer be written"
