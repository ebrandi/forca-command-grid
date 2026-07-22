"""KB-38 — apply a deploy profile's feature-flag preset (WS-D5).

The member-facing feature flags are stored in the database (``core.features`` — a single
``AppSetting`` disabled-set row), not in settings, so a "killboard-first" deploy profile
cannot be expressed purely by an env var. This command is the mechanism: it flips the DB
flags to match a named profile.

    manage.py apply_profile               # use settings.FORCA_PROFILE (default "full")
    manage.py apply_profile killboard     # killboard + its intel tools + market pricing only
    manage.py apply_profile full          # the whole suite (clears every disable)
    manage.py apply_profile killboard --dry-run

Profiles:
  * **full** — everything on (the default; clears the disabled set).
  * **killboard** — keep the corp killboard, its intel tools and market pricing on; turn the
    heavier ERP/industry/command modules off. This is a land-and-expand starting point, not a
    lock: leadership can re-enable any module afterwards on the Features admin page, and a
    later ``apply_profile full`` restores everything.

Nothing here is hardcoded into settings, so the full-suite default is never broken — a corp
that never runs this command sees the historical default (everything on).
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# The features a killboard-first instance keeps ON. Everything else in core.features.FEATURES
# is disabled by the preset. SSO, the SDE and name resolution are infrastructure (not toggleable
# features), so they always run regardless of profile.
KILLBOARD_PROFILE_KEEP = {
    "killboard",     # the board, rankings, analytics, gamification
    "hall_of_fame",  # the community leaderboard fed by killboard data
    "intel",         # watchlists, roaming targets, the scan analyzer
    "market",        # market pricing — the ISK valuation the board needs
}

PROFILES = ("full", "killboard")


class Command(BaseCommand):
    help = "Apply a deploy profile's feature-flag preset (killboard-first or full suite)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "profile", nargs="?", default=None,
            help="Profile name (killboard|full). Defaults to settings.FORCA_PROFILE.",
        )
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change without writing.")

    def handle(self, *args, **opts) -> None:
        from core.features import (
            AUDIENCE_DISABLED,
            AUDIENCE_FEATURES,
            FEATURES,
            disabled_set,
            set_disabled,
            set_feature_audiences,
        )

        profile = (opts["profile"] or getattr(settings, "FORCA_PROFILE", "full") or "full").lower()
        if profile not in PROFILES:
            raise CommandError(
                f"Unknown profile {profile!r}; choose one of: {', '.join(PROFILES)}."
            )

        all_keys = {f.key for f in FEATURES}
        if profile == "full":
            target_disabled: set[str] = set()
            # Restore each audience-feature to its historical default (corp / public).
            target_audiences = dict(AUDIENCE_FEATURES)
        else:  # killboard-first
            target_disabled = all_keys - KILLBOARD_PROFILE_KEEP
            # Audience-controlled features (doctrines, navigation, raffle) live in a SEPARATE
            # store (features.audience); set_disabled alone would leave them visible, so a
            # killboard-first profile must also drive them to "disabled" unless kept.
            target_audiences = {
                key: (AUDIENCE_FEATURES[key] if key in KILLBOARD_PROFILE_KEEP else AUDIENCE_DISABLED)
                for key in AUDIENCE_FEATURES
            }

        current = disabled_set()
        will_enable = sorted(current - target_disabled)
        will_disable = sorted(target_disabled - current)

        self.stdout.write(f"Profile: {profile}")
        self.stdout.write(
            "Keeping ON: "
            + (", ".join(sorted(all_keys - target_disabled)) if target_disabled else "everything")
        )
        if will_disable:
            self.stdout.write(self.style.WARNING("Turning OFF: " + ", ".join(will_disable)))
        if will_enable:
            self.stdout.write(self.style.SUCCESS("Turning ON: " + ", ".join(will_enable)))
        if not will_disable and not will_enable:
            self.stdout.write("No change — flags already match this profile.")

        if opts["dry_run"]:
            self.stdout.write("(dry run — nothing written)")
            return

        set_disabled(target_disabled)
        set_feature_audiences(target_audiences)
        self.stdout.write(self.style.SUCCESS(f"Applied the {profile!r} profile."))
