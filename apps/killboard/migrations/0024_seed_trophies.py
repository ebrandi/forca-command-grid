"""KB-37 (WS-D3) — seed a sensible default trophy catalogue.

Hand-authored, create-only (never clobbers a catalogue leadership has already edited), mirroring
the combat-rank ladder seed (0009). Trophies are DB-configurable from the Admin Console; this just
gives a fresh install a motivating spread across every category from day one. No reward is attached
by default (grants_reward stays off), matching the reward-less rank seed — leadership arms payouts
deliberately.
"""
from __future__ import annotations

from django.db import migrations

# (slug, name, description, category, tier, criteria, color, sort_order)
DEFAULT_TROPHIES = [
    # Kills
    ("first-blood", "First Blood", "Land your first 25 confirmed kills.",
     "kills", "bronze", {"metric": "kills", "threshold": 25}, "text-muted", 10),
    ("executioner", "Executioner", "250 kills — a proven combatant.",
     "kills", "silver", {"metric": "kills", "threshold": 250}, "text-cyan", 11),
    ("warlord", "Warlord", "A thousand kills. The enemy knows your name.",
     "kills", "gold", {"metric": "kills", "threshold": 1000}, "text-kill", 12),
    # Solo
    ("lone-wolf", "Lone Wolf", "10 solo kills — pure pilot skill.",
     "solo", "bronze", {"metric": "solo_kills", "threshold": 10}, "text-muted", 20),
    ("ghost", "Ghost", "100 solo kills. You hunt alone and win.",
     "solo", "gold", {"metric": "solo_kills", "threshold": 100}, "text-kill", 21),
    # Final blows
    ("reaper", "Reaper", "Land 250 final blows — the killing shot is yours.",
     "special", "gold", {"metric": "final_blows", "threshold": 250}, "text-kill", 30),
    # Value
    ("big-game-hunter", "Big Game Hunter", "Get on a single kill worth 1B ISK or more.",
     "value", "silver", {"metric": "kill_value_at_least", "isk": 1_000_000_000}, "text-gold", 40),
    ("whale-slayer", "Whale Slayer", "Get on a single kill worth 10B ISK or more.",
     "value", "gold", {"metric": "kill_value_at_least", "isk": 10_000_000_000}, "text-kill", 41),
    # Ship class
    ("dread-bane", "Dread Bane", "Get on your first capital kill.",
     "ship_class", "silver", {"metric": "ship_class_kills", "class": "Capital", "threshold": 1},
     "text-gold", 50),
    ("line-breaker", "Line Breaker", "50 battleship kills — you break the enemy line.",
     "ship_class", "bronze", {"metric": "ship_class_kills", "class": "Battleship", "threshold": 50},
     "text-muted", 51),
    # Security band
    ("sov-fighter", "Sovereignty Fighter", "50 kills in nullsec.",
     "sec_band", "bronze", {"metric": "sec_band_kills", "band": "nullsec", "threshold": 50},
     "text-muted", 60),
    ("faction-warrior", "Faction Warrior", "50 kills in lowsec.",
     "sec_band", "bronze", {"metric": "sec_band_kills", "band": "lowsec", "threshold": 50},
     "text-muted", 61),
    # Role
    ("guardian-angel", "Guardian Angel", "Get on 25 kills flying dedicated logistics.",
     "role", "silver", {"metric": "role_on_kill", "role": "logi", "threshold": 25},
     "text-cyan", 70),
]


def seed_trophies(apps, schema_editor):
    TrophyDefinition = apps.get_model("killboard", "TrophyDefinition")
    if TrophyDefinition.objects.exists():
        return  # create-only: never clobber a configured/edited catalogue
    TrophyDefinition.objects.bulk_create([
        TrophyDefinition(
            slug=slug, name=name, description=desc, category=category, tier=tier,
            criteria=criteria, color_class=color, sort_order=sort, enabled=True,
            grants_reward=False, reward_type="none", reward_amount=0,
        )
        for (slug, name, desc, category, tier, criteria, color, sort) in DEFAULT_TROPHIES
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("killboard", "0023_gamification"),
    ]

    operations = [
        migrations.RunPython(seed_trophies, migrations.RunPython.noop),
    ]
