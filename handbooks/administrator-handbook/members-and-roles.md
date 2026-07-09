# Members and Roles

## Table of contents

- [How member access works](#how-member-access-works)
- [Auto-Director from the in-game role](#auto-director-from-the-in-game-role)
- [Granting officer, recruiter, or fc](#granting-officer-recruiter-or-fc)
- [Dual control for Director grants](#dual-control-for-director-grants)
- [Character recovery and detach](#character-recovery-and-detach)
- [Troubleshooting access](#troubleshooting-access)

This page is the console-side companion to
[Permissions and roles](../permissions-and-roles.md), which documents the full role model
in detail. Use that page as the reference; this one focuses on what you actually click.

## How member access works

**Member access is automatic.** When a pilot links an EVE character that belongs to the
home corporation, they gain the application's `member` role on the next sync — nothing to
grant by hand. When that pilot leaves the corporation in-game, member access is removed
just as automatically on the next sync. You don't manage individual member grants; you
manage the exceptions above member level.

## Auto-Director from the in-game role

A pilot holding the in-game **Director** corporation role is automatically granted the
application's Director role, both at their next login and on a periodic reconcile job.
There's nothing to configure for this to work correctly — it just needs the roster and
role-reconcile background jobs to be running (check `/ops/health/` if a new Director
doesn't show up promptly).

## Granting officer, recruiter, or fc

Officer rank, and the two lateral capabilities, are granted from
**Admin Console → Members & roles** (`/ops/admin/members/`):

- **Officer** — full line-leadership rank: officer boards, most console pages, and every
  capability implied by that rank.
- **`recruiter`** (grants `recruitment.manage`) — lets a member work the recruitment
  candidate pipeline without giving them officer rank or any other officer privilege.
- **`fc`** (grants `fleet.manage`) — lets a member create and run fleet operations without
  full officer rank.

These lateral roles exist precisely so you can trust a member with one workflow — running
fleets, or working recruitment — without handing them everything an officer can do.

## Dual control for Director grants

Granting the **Director** role is deliberately harder than granting officer: it requires a
**second director's approval** before it takes effect (`/ops/admin/role-requests/`). This
means a single compromised or careless director account can never unilaterally mint
another director. Revocations are not subject to the same delay — they apply immediately —
and the app enforces a "last director" floor so the directorate can never be revoked down
to zero and lock the corp out of its own console.

## Character recovery and detach

**Access governance → Character recovery** (`/ops/admin/access/recovery/`) lets a director
detach a character from an account — for example, after an unauthorised transfer or an
account-sharing dispute — cleanly removing its linkage without touching the rest of that
pilot's data.

## Troubleshooting access

| Symptom | Likely cause | Action |
|---|---|---|
| A member sees "not enabled" or a 404 on a feature | The feature is disabled, or its audience excludes them | Check [Features and audiences](./features-and-audiences.md) |
| A logged-in pilot is bounced to onboarding | They hold no `member` role (not in the home corporation) | Confirm corp membership in-game; wait for or trigger a roster sync |
| A new in-game Director lacks Director access | The role-reconcile job hasn't run yet | Check `/ops/health/`, or have them sign out and back in |
| Granting Director "does nothing" | It's awaiting a second director's approval | Have another director approve it at `/ops/admin/role-requests/` |

For the full role model — rank tiers, lateral capabilities, and how permissions are
enforced in code — see [Permissions and roles](../permissions-and-roles.md).
