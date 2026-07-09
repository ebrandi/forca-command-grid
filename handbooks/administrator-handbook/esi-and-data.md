# ESI and Data

## Table of contents

- [Director scopes and the dashboards they power](#director-scopes-and-the-dashboards-they-power)
- [Granting a corp scope](#granting-a-corp-scope)
- [Pilots who haven't authorised ESI](#pilots-who-havent-authorised-esi)
- [Data retention policy](#data-retention-policy)
- [The member-leave sweep](#the-member-leave-sweep)
- [Troubleshooting empty corp data](#troubleshooting-empty-corp-data)

## Director scopes and the dashboards they power

Corp-wide dashboards are only as complete as the ESI scopes a director has granted. Each
one requires a specific in-game corporation role on the authorising character, not just
the application's Director rank:

| Dashboard / feature | Scope(s) | Required in-game role |
|---|---|---|
| Roster with location/ship/last login | `esi-corporations.read_corporation_membership.v1`, `esi-corporations.track_members.v1` | Director |
| Corp wallet balances and journal | `esi-wallet.read_corporation_wallets.v1` | Accountant or Director |
| Structure fuel/state/timers | `esi-corporations.read_structures.v1`, `esi-universe.read_structures.v1` | Director or Station Manager |
| Corp contacts / standings board | `esi-corporations.read_contacts.v1` | Director |
| Moon extraction calendar / mining ledger | `esi-industry.read_corporation_mining.v1` | Station Manager or Director |
| Corp assets / stockpile | `esi-assets.read_corporation_assets.v1`, `esi-universe.read_structures.v1` | Director |
| Corp blueprints and industry jobs | `esi-corporations.read_blueprints.v1`, `esi-industry.read_corporation_jobs.v1` | Director or Factory Manager |
| Corp contracts oversight | `esi-contracts.read_corporation_contracts.v1` | Director |
| Ansiblex + cyno jump network | `esi-corporations.read_structures.v1`, `esi-universe.read_structures.v1` | Director or Station Manager |
| Corp killmail feed | `esi-killmails.read_corporation_killmails.v1` | (in-game Director token) |
| Notification relay | `esi-characters.read_notifications.v1` | Director or role-holder |
| Doctrine import from saved fits | `esi-fittings.read_fittings.v1` | Director |

This table mirrors the full scope catalogue in
[Permissions and roles: ESI scopes](../permissions-and-roles.md#esi-scopes) — that page is
the canonical reference; this one groups the same scopes by "what stops working without
it" so you can diagnose a blank dashboard quickly.

## Granting a corp scope

Corp-wide scopes are granted the same way personal ones are — from the
**ESI Scopes page** (`/auth/eve/scopes/`) — but only take effect for corp-wide dashboards
when the character granting them **also holds the matching in-game corporation role**
(shown above). Practically:

1. Identify which in-game role the target dashboard needs (see the table above).
2. Have a director whose character holds that role sign in and open the ESI Scopes page.
3. Grant the relevant scope for that character.
4. The next scheduled sync for that data area will pick it up; most subsystems also offer
   a "sync now" action on their own page for an immediate pull.

Corp syncs are harmless no-ops until the matching scope exists — there's no risk in
enabling a dashboard before you're ready to fully rely on it.

## Pilots who haven't authorised ESI

A member who has signed in gets the baseline login scopes automatically, but optional
personal scopes (assets, industry jobs, contracts, fittings, PI colonies) are opt-in per
pilot. If a member's personal data is missing from a feature that needs one of these, it's
because they haven't granted it themselves — this isn't something leadership can grant on
a pilot's behalf. Point them at
[Account and ESI](../end-user-guide/account-and-esi.md) in the End-User Guide.

## Data retention policy

Retention windows for historical and operational data are configured at
**Admin Console → Retention** (`/ops/admin/retention/settings/`) and enforced nightly.

## The member-leave sweep

A separate nightly job applies the corp's on-departure retention policy to a pilot's data
once they leave the home corporation. It ships **disarmed** — report-only, writing a
report but deleting nothing — until a director explicitly arms it from the retention page.
Review the reports it produces before arming deletion, so you understand exactly what will
be removed once it's live.

## Troubleshooting empty corp data

| Symptom | Likely cause | Action |
|---|---|---|
| Corp assets, wallet, structures, or contacts are empty | No director has granted the matching scope | Grant it from a character holding the required in-game role (see the table above) |
| Roster looks incomplete or stale | The roster sync hasn't run, or the granting character's token expired | Check `/ops/health/`; have the granting director re-authorise |
| Mining ledger is empty | The `moon_mining` scope hasn't been granted, or there's no refinery observer data yet | Grant the scope from a Station Manager or Director character |
| A member's personal data (assets, industry jobs) is missing | That pilot hasn't opted into the personal scope | This is self-service on the pilot's side — see [Account and ESI](../end-user-guide/account-and-esi.md) |

For the full data model — what's collected, how tokens are encrypted, and member data
rights — see [Data and privacy](../data-and-privacy.md).
