"""The `effpct` efficiency-display filter — the single, consistent formatter for the
killboard's ISK-efficiency KPI.

Bug it fixes: a pilot with real losses (e.g. 9,769 kills / 183 losses → 99.56% true
efficiency) rendered as a misleading "100%" because `floatformat:0`/`:1` rounds up.
A pilot with any ISK lost must never read as a perfect 100%.
"""
from __future__ import annotations

import pytest
from django.template import Context, Template

from apps.sde.templatetags.eve import effpct


@pytest.mark.parametrize("value,expected", [
    (100.0, "100"),          # truly lossless → the only way to show 100
    (100, "100"),
    (150.0, "100"),          # clamp any overshoot
    (99.5591, "99.6"),       # the real prod pilot — was "100", now honest
    (99.97, "99.9"),         # would round up to 100 → capped just below
    (99.95, "99.9"),
    (99.99, "99.9"),
    (99.0, "99.0"),
    (75.44, "75.4"),
    (50.0, "50.0"),
    (0.04, "0.0"),
    (0, "0"),
    (0.0, "0"),
    (-5.0, "0"),             # nonsensical negative → 0
    (None, "0"),
    ("not-a-number", "0"),
])
def test_effpct_never_rounds_up_to_100(value, expected):
    assert effpct(value) == expected


def test_effpct_registered_in_eve_lib():
    """It must be usable as a template filter (loaded via {% load eve %})."""
    t = Template("{% load eve %}{{ v|effpct }}%")
    assert t.render(Context({"v": 99.5591})) == "99.6%"
    assert t.render(Context({"v": 100.0})) == "100%"


def test_lossy_pilot_never_shows_100(django_user_model):
    """End-to-end: a pilot with losses but very high ISK efficiency renders < 100%
    on the roster, never a misleading 100%."""
    from apps.killboard import leaderboards
    from tests._raffle_utils import HOME_CORP, enrol_pilot, home_kill

    _user, _char = enrol_pilot(django_user_model, 4242)
    # Many high-value kills, one cheap loss → efficiency ~99.x%, not 100%.
    for i in range(40):
        home_kill(70000 + i, attackers=[(4242, HOME_CORP, True)], value="1000000000")
    # A real loss (home corp as victim) worth far less than the kills.
    from decimal import Decimal

    from django.utils import timezone

    from apps.killboard.models import Killmail
    Killmail.objects.create(
        killmail_id=79999, killmail_hash="hloss", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587, total_value=Decimal("5000000"),
        is_npc=False, involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_character_id=4242, victim_corporation_id=HOME_CORP,
    )
    roster = leaderboards.corp_combat_roster(use_cache=False)
    me = next(p for p in roster if p["character_id"] == 4242)
    assert me["losses"] == 1
    assert 99.0 < me["efficiency"] < 100.0        # true value is < 100
    assert effpct(me["efficiency"]) != "100"      # and it never displays as 100
